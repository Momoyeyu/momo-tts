import base64
import io
import tempfile
import unittest
import wave
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from momo_tts import gateway, worker

KEY = b"momo-index-test-key-not-used-by-deployment"
AUTH = {"Authorization": f"Bearer {KEY.decode()}"}
PAYLOAD = {
    "input": "こんにちは。",
    "language": "ja",
    "reference_audio": base64.b64encode(b"synthetic fixture").decode(),
    "reference_name": "../reference.wav",
}


class TestWorker(unittest.TestCase):
    def test_authorized_synthesis_and_ephemeral_reference(self):
        paths = []

        class Engine:
            def infer(self, *, spk_audio_prompt, text, lang, output_path):
                assert text == PAYLOAD["input"] and lang == "ja"
                reference = Path(spk_audio_prompt)
                assert reference.name == "reference.wav"
                assert reference.read_bytes() == b"synthetic fixture"
                paths.append(reference.parent)
                with wave.open(output_path, "wb") as wav:
                    wav.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
                    wav.writeframes(b"\x01\x02" * 100)

        with TestClient(worker.create_app(load=Engine, key=KEY)) as client:
            self.assertEqual(client.get("/readyz").status_code, 401)
            self.assertTrue(client.get("/readyz", headers=AUTH).json()["ready"])
            failed = client.post("/v1/audio/speech", json={**PAYLOAD, "reference_audio": "%"}, headers=AUTH)
            self.assertEqual(failed.status_code, 422)
            response = client.post("/v1/audio/speech", json=PAYLOAD, headers=AUTH)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Content-Type"], "audio/wav")
            with wave.open(io.BytesIO(response.content)) as wav:
                self.assertEqual(wav.getframerate(), 22050)
            self.assertEqual(len(paths), 1)
            self.assertFalse(paths[0].exists())


class TestGateway(unittest.TestCase):
    def test_live_slot_switch_and_auth(self):
        def route(request):
            assert request.headers["Authorization"] == AUTH["Authorization"]
            return httpx.Response(200, json={"ready": True, "port": request.url.port})

        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            for slot in gateway.SLOTS:
                (directory / f"{slot}.lock").touch()
            with TestClient(gateway.create_app(directory, KEY, httpx.MockTransport(route))) as client:
                self.assertTrue(client.get("/healthz").json()["alive"])
                self.assertEqual(client.get("/readyz").status_code, 401)
                self.assertEqual(client.get("/readyz", headers=AUTH).status_code, 503)
                for slot in gateway.SLOTS:
                    temporary = directory / "active.tmp"
                    temporary.write_text(slot)
                    temporary.replace(directory / "active")
                    response = client.get("/readyz", headers=AUTH)
                    self.assertEqual(response.json(), {"ready": True, "port": int(slot)})

    def test_switch_drains_an_inflight_request(self):
        import asyncio
        import fcntl
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        entered = Event()
        finish = Event()

        async def route(request):
            if request.url.port == 19881:
                entered.set()
                assert await asyncio.to_thread(finish.wait, 3)
            return httpx.Response(200, json={"port": request.url.port})

        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            for slot in gateway.SLOTS:
                (directory / f"{slot}.lock").touch()
            active = directory / "active"
            active.write_text("19881")
            with TestClient(gateway.create_app(directory, KEY, httpx.MockTransport(route))) as client:
                lease = (directory / "19881.lock").open("rb")
                with lease, ThreadPoolExecutor(max_workers=1) as thread:
                    old_request = thread.submit(client.post, "/v1/audio/speech", json=PAYLOAD, headers=AUTH)
                    self.assertTrue(entered.wait(2))
                    try:
                        new_pointer = directory / "active.new"
                        new_pointer.write_text("19882")
                        new_pointer.replace(active)
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        self.assertEqual(client.get("/readyz", headers=AUTH).json(), {"port": 19882})
                    finally:
                        finish.set()
                    self.assertEqual(old_request.result(3).json(), {"port": 19881})
                    self.assertEqual(client.get("/readyz", headers=AUTH).json(), {"port": 19882})
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
