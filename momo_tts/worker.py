import asyncio
import base64
import io
import json
import logging
import os
import tempfile
import wave
from contextlib import asynccontextmanager, redirect_stdout
from pathlib import Path
from typing import Literal

from anyio import to_thread
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

from .api import MAX_REFERENCE, MAX_WAV, authorize, read_body, read_key

LOG = logging.getLogger(__name__)
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"}


class Speech(BaseModel):
    input: str = Field(min_length=1, max_length=2000)
    language: Literal["zh", "en", "ja"]
    reference_audio: str
    reference_name: str = Field(min_length=1, max_length=255)


def load_engine():
    from indextts.infer_v2_5 import IndexTTS2

    model_dir = Path(os.environ["MOMO_INDEX_MODELS"])
    return IndexTTS2(
        cfg_path=str(model_dir / "config.yaml"),
        model_dir=str(model_dir),
        device="cuda:0",
        use_bf16=True,
        use_cuda_kernel=False,
    )


def render(engine, speech: Speech, suffix: str, reference: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="speech-") as scratch:
        source = Path(scratch) / ("reference" + suffix)
        target = Path(scratch) / "output.wav"
        source.write_bytes(reference)
        # Upstream prints normalized dialogue to stdout even with verbose=False.
        with open(os.devnull, "w") as sink, redirect_stdout(sink):
            engine.infer(
                spk_audio_prompt=str(source),
                text=speech.input,
                lang=speech.language,
                output_path=str(target),
            )
        with target.open("rb") as output:
            audio = output.read(MAX_WAV + 1)
        if len(audio) > MAX_WAV:
            raise ValueError("Output too large")
        with wave.open(io.BytesIO(audio)) as wav:
            if (wav.getnchannels(), wav.getsampwidth()) != (1, 2) or not wav.getnframes():
                raise ValueError("Invalid synthesized audio")
        return audio


def create_app(load=load_engine, key=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.key = key if key is not None else read_key()
        app.state.engine = await to_thread.run_sync(load)
        app.state.inference = asyncio.Lock()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/readyz")
    async def ready(request: Request):
        authorize(request, app.state.key)
        return {"ready": True, "engine": "index-tts"}

    @app.post("/v1/audio/speech")
    async def speech(request: Request):
        authorize(request, app.state.key)
        async with app.state.inference:
            raw = await read_body(request)
            try:
                payload = Speech.model_validate(json.loads(raw))
                audio = base64.b64decode(payload.reference_audio, validate=True)
                suffix = Path(payload.reference_name).suffix.lower()
                if not payload.input.strip() or not audio or suffix not in AUDIO_SUFFIXES:
                    raise ValueError
            except (ValidationError, ValueError) as error:
                raise HTTPException(422, "Invalid text, language or reference audio") from error
            if len(audio) > MAX_REFERENCE:
                raise HTTPException(413, "Reference audio too large")
            try:
                result = await to_thread.run_sync(render, app.state.engine, payload, suffix, audio)
            except Exception as error:
                LOG.error("Synthesis failed (%s)", type(error).__name__)
                raise HTTPException(502, "IndexTTS synthesis failed") from error
        return Response(result, media_type="audio/wav")

    return app
