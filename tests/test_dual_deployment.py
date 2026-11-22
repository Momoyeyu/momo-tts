import fcntl
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from momo_tts import gateway
from momo_tts.protocol import ENGINES


class TestDualGateway(unittest.TestCase):
    def test_gpt_preserves_stream_bytes_and_holds_slot_lease(self):
        key = b"fixture-gpt-key"
        received = []
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            lock = directory / "19885.lock"
            lock.touch()
            (directory / "active").write_text("19885")

            class Audio(httpx.AsyncByteStream):
                async def __aiter__(self):
                    with lock.open("rb") as candidate:
                        with self_case.assertRaises(BlockingIOError):
                            fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    yield b"RIFF streaming header"
                    yield b"PCM payload"

            self_case = self

            def worker(request):
                received.append((request.url.port, request.url.path, request.headers["Authorization"]))
                return httpx.Response(200, stream=Audio(), headers={"Content-Type": "audio/wav"})

            app = gateway.create_app(directory, key, httpx.MockTransport(worker), engine="gpt")
            with TestClient(app) as client:
                self.assertEqual(client.post("/tts", json={}).status_code, 401)
                self.assertEqual(client.post("/v1/audio/speech", json={}).status_code, 404)
                response = client.post("/tts", json={}, headers={"Authorization": "Bearer fixture-gpt-key"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b"RIFF streaming headerPCM payload")
                self.assertEqual(received, [(19885, "/tts", "Bearer fixture-gpt-key")])
                with lock.open("rb") as candidate:
                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertNotEqual(ENGINES["index"].slots, ENGINES["gpt"].slots)


class TestDeployment(unittest.TestCase):
    def test_bad_candidate_restores_spare_without_touching_live_worker(self):
        from unittest.mock import patch
        from momo_tts.control import Installation

        with tempfile.TemporaryDirectory() as temp, patch.object(Path, "home", return_value=Path(temp)):
            install = Installation("gpt")
            current, previous, next_revision = ("a" * 40, "b" * 40, "c" * 40)
            settings = {"MOMO_TTS_DATA": temp, "MOMO_TTS_KEY_FILE": str(install.key_file)}
            (install.state / "slots").mkdir(parents=True)
            (install.state / "active").write_text("19885")
            (install.state / "previous-release").write_text(previous)
            releases = install.state / "releases"
            install.point("19885", releases / current)
            install.point("19886", releases / previous)
            with (
                patch("momo_tts.control.active", return_value=True),
                patch("momo_tts.control.run") as run,
                patch("momo_tts.control.subprocess.run"),
                patch("momo_tts.control.read_key", return_value=b"fixture"),
                patch("momo_tts.control.load_reference", return_value={}),
                patch("momo_tts.control.wait_ready"),
                patch("momo_tts.control.synthesize", side_effect=RuntimeError("no synthesis")),
                patch.object(install, "retire") as retire,
                patch.object(install, "stage", return_value=releases / next_revision),
            ):
                run.return_value.stdout = next_revision
                with self.assertRaisesRegex(RuntimeError, "Live endpoint unchanged"):
                    install.switch("deploy", settings)
                self.assertTrue(all(call.args == ("19886",) for call in retire.call_args_list))
            self.assertEqual(install.active_slot(), "19885")
            self.assertEqual(install.revision("19885"), current)
            self.assertEqual(install.revision("19886"), previous)
            self.assertEqual((install.state / "previous-release").read_text(), previous)


class TestResidency(unittest.IsolatedAsyncioTestCase):
    async def test_idle_retirement_waits_for_sessions_and_streams(self):
        from unittest.mock import AsyncMock
        from uuid import uuid4
        from momo_tts.residency import Residency

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = directory / "19885.lock"
            path.touch()
            residency = Residency("gpt", directory, b"fixture", None)
            residency.command = AsyncMock(return_value=0)
            session = uuid4()
            residency.renew(session)
            slot = lambda: "19885"
            await residency.retire_idle(slot)
            residency.command.assert_not_awaited()
            residency.release(session)
            with path.open("rb") as lock:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                await residency.retire_idle(slot)
                residency.command.assert_not_awaited()
            await residency.retire_idle(slot)
            residency.command.assert_any_await("stop", "19885")
