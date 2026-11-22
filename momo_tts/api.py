import hmac
import os
from pathlib import Path

from fastapi import HTTPException, Request

MAX_REFERENCE = 10 * 1024 * 1024
MAX_BODY = ((MAX_REFERENCE + 2) // 3) * 4 + 32768
MAX_WAV = 32 * 1024 * 1024
SLOTS = ("19881", "19882")


def read_key() -> bytes:
    key = Path(os.environ["MOMO_INDEX_KEY_FILE"]).read_bytes().strip()
    if len(key) < 32 or any(byte not in range(33, 127) for byte in key):
        raise RuntimeError("The service key must contain at least 32 printable ASCII characters.")
    return key


def authorize(request: Request, key: bytes) -> None:
    received = request.headers.get("Authorization", "").encode("latin-1")
    if not hmac.compare_digest(received, b"Bearer " + key):
        raise HTTPException(401, "Authorization required")


async def read_body(request: Request) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BODY:
            raise HTTPException(413, "Request too large")
        data.extend(chunk)
    return bytes(data)
