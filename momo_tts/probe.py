import base64
import io
import json
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

from .protocol import ENGINES, MAX_REFERENCE, MAX_WAV

LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SAMPLES = {"zh": "你好，欢迎回来。", "en": "Hello, it's good to see you again.", "ja": "こんにちは、おかえりなさい。"}


def request(port, route, key, payload=None, timeout=4):
    outgoing = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}",
        json.dumps(payload).encode("utf-8") if payload is not None else None,
        {"Authorization": "Bearer " + key.decode("ascii"), "Content-Type": "application/json"},
    )
    return LOOPBACK_OPENER.open(outgoing, timeout=timeout)


def remote_status(port, key):
    try:
        with request(port, "/readyz", key) as response:
            value = json.load(response)
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def ready(port, key):
    return remote_status(port, key).get("ready") is True


def wait_ready(port, key, seconds=0):
    deadline = time.monotonic() + seconds
    while not ready(port, key):
        if time.monotonic() >= deadline:
            raise RuntimeError("Inference worker did not become ready")
        time.sleep(1)


def load_reference(data: Path, name: str):
    bundle = data / "smoke/reference.json"
    if bundle.is_file():
        return json.loads(bundle.read_text())
    # Keep existing IndexTTS deployments working before an operator replaces the test audio.
    legacy = data / "smoke/reference.wav"
    if name == "index" and legacy.is_file():
        return {"audio": base64.b64encode(legacy.read_bytes()).decode(), "prompt": "", "language": "zh"}
    raise RuntimeError("Set a validation reference with make set-reference first")


def make_reference(path, prompt, language):
    if language not in SAMPLES:
        raise RuntimeError("Reference language must be zh, en or ja")
    with path.open("rb") as reference:
        audio = reference.read(MAX_REFERENCE + 1)
    if len(audio) > MAX_REFERENCE or len(prompt) > 1000:
        raise RuntimeError("Reference audio or transcript is too large")
    try:
        with wave.open(io.BytesIO(audio)) as wav:
            duration = wav.getnframes() / wav.getframerate()
            frames = wav.readframes(wav.getnframes())
            if (wav.getnchannels(), wav.getsampwidth()) != (1, 2) or not 3 <= duration <= 10:
                raise RuntimeError("Use a mono PCM16 WAV lasting 3–10 seconds")
            if len(frames) != wav.getnframes() * 2 or not any(frames):
                raise RuntimeError("Reference audio is incomplete or silent")
    except (wave.Error, EOFError) as error:
        raise RuntimeError("Invalid WAV reference") from error
    return {"audio": base64.b64encode(audio).decode(), "prompt": prompt.strip(), "language": language}


def synthesize(name, port, key, reference, language="zh"):
    text = SAMPLES[language]
    if name == "gpt":
        payload = {
            "text": text, "text_lang": language, "ref_audio": reference["audio"],
            "ref_audio_name": "reference.wav", "prompt_lang": reference["language"],
            "prompt_text": reference["prompt"], "media_type": "wav", "streaming_mode": False,
        }
    else:
        payload = {
            "input": text, "language": language, "reference_audio": reference["audio"],
            "reference_name": "reference.wav",
        }
    try:
        with request(port, ENGINES[name].speech, key, payload, timeout=115) as response:
            audio = response.read(MAX_WAV + 1)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Synthesis failed (HTTP {error.code})") from None
    try:
        if len(audio) > MAX_WAV:
            raise RuntimeError("Audio response exceeds the size limit")
        with wave.open(io.BytesIO(audio)) as wav:
            duration = wav.getnframes() / wav.getframerate()
            frames = wav.readframes(wav.getnframes())
            if (wav.getnchannels(), wav.getsampwidth()) != (1, 2) or duration < 0.25:
                raise RuntimeError("Invalid synthesis result")
            if len(frames) != wav.getnframes() * 2 or not any(frames):
                raise RuntimeError("Silent or incomplete synthesis result")
            print(f"{language}: {wav.getframerate()} Hz, {duration:.2f} s")
    except (wave.Error, EOFError) as error:
        raise RuntimeError("Invalid WAV from inference worker") from error
