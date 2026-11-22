import hmac

from fastapi import HTTPException, Request

from .protocol import MAX_REFERENCE, MAX_WAV, read_key

MAX_BODY = ((MAX_REFERENCE + 2) // 3) * 4 + 65536


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
