from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

MAX_REFERENCE = 10 * 1024 * 1024
MAX_WAV = 32 * 1024 * 1024


@dataclass(frozen=True)
class Endpoint:
    port: str
    slots: tuple[str, str]
    speech: str


ENGINES = {
    "index": Endpoint("19880", ("19881", "19882"), "/v1/audio/speech"),
    "gpt": Endpoint("19884", ("19885", "19886"), "/tts"),
}


def read_key(path: Path | None = None) -> bytes:
    if path is None:
        path = Path(os.environ.get("MOMO_TTS_KEY_FILE") or os.environ["MOMO_INDEX_KEY_FILE"])
    key = path.read_bytes().strip()
    if len(key) < 32 or any(byte not in range(33, 127) for byte in key):
        raise RuntimeError("The service key must contain at least 32 printable ASCII characters.")
    return key
