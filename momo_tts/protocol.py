from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit

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


def admin_origin(value: str) -> str:
    if not value or value == "off":
        return ""
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https" or not url.hostname or url.username is not None
            or url.path not in ("", "/") or url.query or url.fragment
            or url.port == 0 or any(char.isspace() for char in value)
        ):
            raise ValueError
    except ValueError:
        raise ValueError("ADMIN_ORIGIN must be an HTTPS origin such as https://console.example.com") from None
    # Browsers omit the default HTTPS port from Host and Origin.
    authority = url.netloc if url.port != 443 else url.netloc.rsplit(":", 1)[0]
    return f"https://{authority.lower()}"
