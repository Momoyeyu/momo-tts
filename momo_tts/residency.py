"""Admission, bounded startup and idle retirement for one inference engine."""

import asyncio
from contextlib import suppress
import fcntl
import time
from uuid import UUID

import httpx
from fastapi import HTTPException

SESSION_TTL = 45
IDLE_TTL = 12


class Residency:
    def __init__(self, engine, directory, key, client):
        self.engine = engine
        self.directory = directory
        self.key = key
        self.client = client
        self.sessions = {}
        self.starts = {}
        self.last_use = 0.0
        self.released = False

    def session_count(self, now=None):
        now = time.monotonic() if now is None else now
        self.sessions = {session: expiry for session, expiry in self.sessions.items() if expiry > now}
        return len(self.sessions)

    def renew(self, session: UUID):
        if self.session_count() >= 128 and session not in self.sessions:
            raise HTTPException(429, "Too many connected clients")
        self.sessions[session] = time.monotonic() + SESSION_TTL
        self.released = False

    def release(self, session: UUID):
        if self.sessions.pop(session, None) is not None and not self.session_count():
            self.last_use = 0.0
            self.released = True

    async def command(self, operation, slot):
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", operation, f"momo-{self.engine}-worker@{slot}.service",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(145):
                return await proc.wait()
        finally:
            if proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

    async def loaded(self, slot):
        try:
            reply = await self.client.get(
                f"http://127.0.0.1:{slot}/readyz", timeout=1,
                headers={"Authorization": "Bearer " + self.key.decode("ascii")},
            )
            return reply.is_success and reply.json().get("ready") is True
        except (httpx.HTTPError, ValueError):
            return False

    async def _wake(self, slot):
        if await self.loaded(slot):
            return
        if await self.command("start", slot):
            raise HTTPException(503, "Unable to start the inference worker")
        deadline = time.monotonic() + 105
        while not await self.loaded(slot):
            if time.monotonic() >= deadline:
                raise HTTPException(503, "Inference worker failed to warm up")
            await asyncio.sleep(.5)

    async def ensure(self, slot):
        self.used()
        startup = self.starts.get(slot)
        if startup is None or startup.done():
            startup = asyncio.create_task(self._wake(slot))
            # A disconnected requester may leave the model loading. Consume an
            # unobserved exception, while all current waiters still see it.
            startup.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            self.starts[slot] = startup
        await asyncio.shield(startup)

    def used(self):
        self.released = False
        self.last_use = time.monotonic()

    def finished(self):
        if not self.released:
            self.last_use = time.monotonic()

    async def sweep(self, active_slot):
        while True:
            await asyncio.sleep(2)
            await self.retire_idle(active_slot)

    async def retire_idle(self, active_slot):
        def busy():
            return self.session_count() or time.monotonic() - self.last_use < IDLE_TTL or any(
                not pending.done() for pending in self.starts.values()
            )

        slot = active_slot()
        if not slot or busy():
            return
        lock = self.directory / f"{slot}.lock"
        try:
            with lock.open("rb") as lease:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if busy() or slot != active_slot():
                    return
                # is-active includes models currently loading after a release check.
                if await self.command("is-active", slot) == 0 and not busy():
                    await self.command("stop", slot)
        except (FileNotFoundError, BlockingIOError, TimeoutError):
            return

    async def close(self):
        for task in self.starts.values():
            task.cancel()
        await asyncio.gather(*self.starts.values(), return_exceptions=True)
