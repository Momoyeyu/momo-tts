import fcntl
import os
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

from .api import MAX_WAV, SLOTS, authorize, read_body, read_key


def active_slot(directory: Path) -> str | None:
    try:
        slot = (directory / "active").read_text().strip()
    except FileNotFoundError:
        return None
    return slot if slot in SLOTS else None


@contextmanager
def lease_active_slot(directory: Path):
    while slot := active_slot(directory):
        with (directory / f"{slot}.lock").open("rb") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                if slot != active_slot(directory):
                    continue
                raise HTTPException(503, "Active inference worker is busy") from None
            # The active pointer can change between reading it and acquiring the lease.
            if slot == active_slot(directory):
                yield slot
                return
    raise HTTPException(503, "No active inference worker")


def create_app(state_directory=None, key=None, transport=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.directory = state_directory or Path(os.environ["MOMO_INDEX_STATE"])
        app.state.key = key if key is not None else read_key()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(110, connect=2), trust_env=False, transport=transport
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def health():
        return {"alive": True}

    @app.get("/readyz")
    @app.post("/v1/audio/speech")
    async def forward(request: Request):
        authorize(request, app.state.key)
        data = await read_body(request) if request.method == "POST" else None
        path = request.url.path
        timeout = 3 if request.method == "GET" else 110
        headers = {"Authorization": request.headers["Authorization"], "Content-Type": "application/json"}
        with lease_active_slot(app.state.directory) as slot:
            try:
                async with app.state.client.stream(
                    request.method, f"http://127.0.0.1:{slot}{path}",
                    content=data, headers=headers, timeout=timeout,
                ) as upstream:
                    audio = bytearray()
                    async for chunk in upstream.aiter_bytes():
                        if len(audio) + len(chunk) > MAX_WAV:
                            raise HTTPException(502, "Worker response too large")
                        audio.extend(chunk)
                    return Response(
                        bytes(audio), status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type"),
                    )
            except httpx.HTTPError as error:
                raise HTTPException(503, "Inference worker unavailable") from error

    return app
