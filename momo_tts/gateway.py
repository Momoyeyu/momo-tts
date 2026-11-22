import asyncio
import fcntl
import json
import os
from contextlib import ExitStack, asynccontextmanager, contextmanager, suppress
from uuid import UUID
from pathlib import Path

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from starlette.responses import StreamingResponse

from .api import authorize, read_body
from .protocol import ENGINES, MAX_WAV, read_key
from .residency import Residency, SESSION_TTL

SLOTS = ENGINES["index"].slots


def active_slot(directory: Path, slots=SLOTS) -> str | None:
    try:
        slot = (directory / "active").read_text().strip()
    except FileNotFoundError:
        return None
    return slot if slot in slots else None


@contextmanager
def lease_active_slot(directory: Path, slots=SLOTS):
    while slot := active_slot(directory, slots):
        with (directory / f"{slot}.lock").open("rb") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                if slot != active_slot(directory, slots):
                    continue
                raise HTTPException(503, "Active inference worker is busy") from None
            if slot == active_slot(directory, slots):
                yield slot
                return
    raise HTTPException(503, "No active inference worker")


async def bounded_body(upstream):
    size = 0
    async for chunk in upstream.aiter_bytes():
        size += len(chunk)
        if size > MAX_WAV:
            raise HTTPException(502, "Worker response too large")
        yield chunk


class LeasedSpeech(StreamingResponse):
    def __init__(self, upstream: httpx.Response, lease: ExitStack, residency=None):
        self.upstream = upstream
        self.lease = lease
        self.residency = residency
        super().__init__(
            bounded_body(upstream), status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Closing the client stream releases the upstream response before retirement.
            with self.lease, anyio.CancelScope(shield=True):
                await self.upstream.aclose()
                if self.residency:
                    self.residency.finished()


def create_app(state_directory=None, key=None, transport=None, engine=None, residency_factory=None):
    name = engine or os.environ.get("MOMO_TTS_ENGINE", "index")
    endpoint = ENGINES[name]

    @asynccontextmanager
    async def lifespan(app):
        app.state.directory = state_directory or Path(os.environ.get("MOMO_TTS_STATE") or os.environ["MOMO_INDEX_STATE"])
        app.state.key = key if key is not None else read_key()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(110, connect=2), trust_env=False, transport=transport
        ) as client:
            app.state.client = client
            factory = residency_factory or (Residency if transport is None else None)
            app.state.residency = factory(name, app.state.directory, app.state.key, client) if factory else None
            sweep = None
            if app.state.residency:
                sweep = asyncio.create_task(app.state.residency.sweep(
                    lambda: active_slot(app.state.directory, endpoint.slots)
                ))
            try:
                yield
            finally:
                if sweep:
                    sweep.cancel()
                    with suppress(asyncio.CancelledError):
                        await sweep
                    await app.state.residency.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def health():
        return {"alive": True}

    @app.put("/session/{session}")
    async def heartbeat(request: Request, session: UUID):
        authorize(request, app.state.key)
        if app.state.residency:
            app.state.residency.renew(session)
        return {"expiresIn": SESSION_TTL}

    @app.delete("/session/{session}", status_code=204)
    async def release(request: Request, session: UUID):
        authorize(request, app.state.key)
        if app.state.residency:
            app.state.residency.release(session)
        return Response(status_code=204)

    @app.get("/readyz")
    @app.post(endpoint.speech)
    async def forward(request: Request):
        authorize(request, app.state.key)
        residency = app.state.residency
        if request.method == "GET" and residency:
            slot = active_slot(app.state.directory, endpoint.slots)
            configured = bool(slot and (app.state.directory / "slots" / slot).is_dir())
            return Response(
                json.dumps({"ready": configured, "resident": configured and await residency.loaded(slot),
                            "sessions": residency.session_count()}),
                status_code=200 if configured else 503, media_type="application/json",
            )
        data = await read_body(request) if request.method == "POST" else None
        timeout = 3 if request.method == "GET" else 110
        headers = {"Authorization": request.headers["Authorization"], "Content-Type": "application/json"}
        with ExitStack() as lease:
            for attempt in range(100):
                try:
                    slot = lease.enter_context(lease_active_slot(app.state.directory, endpoint.slots))
                    break
                except HTTPException as error:
                    if error.detail != "Active inference worker is busy" or attempt == 99:
                        raise
                    await asyncio.sleep(.1)
            client = app.state.client
            url = f"http://127.0.0.1:{slot}{request.url.path}"
            if residency:
                await residency.ensure(slot)
            try:
                outgoing = client.build_request(request.method, url, content=data, headers=headers, timeout=timeout)
                upstream = await client.send(outgoing, stream=True)
                if name == "gpt" and request.method == "POST" and upstream.is_success:
                    return LeasedSpeech(upstream, lease.pop_all(), residency)
                # Status and IndexTTS use a bounded, complete response.
                try:
                    body = bytearray()
                    async for chunk in bounded_body(upstream):
                        body.extend(chunk)
                    return Response(
                        bytes(body), status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type"),
                    )
                finally:
                    with anyio.CancelScope(shield=True):
                        await upstream.aclose()
                    if residency:
                        residency.finished()
            except httpx.HTTPError as error:
                raise HTTPException(503, "Inference worker unavailable") from error

    return app
