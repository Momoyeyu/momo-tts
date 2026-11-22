import asyncio
import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from fastapi.testclient import TestClient

from momo_tts.admin import create_app
from momo_tts.control import DataLayout

KEY = b"local-admin-console-key-for-tests-only"
AUTH = {"Authorization": f"Bearer {KEY.decode()}"}
LOCAL = "http://127.0.0.1:19890"


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.data_root = self.directory / "data"
        self.data_root.mkdir()
        self.layout = DataLayout(self.directory)
        self.layout.select(str(self.data_root))
        self.app = create_app(
            self.directory, self.directory / "state", key=KEY, storage=self.layout
        )

    def client(self, host="127.0.0.1"):
        return TestClient(self.app, base_url=LOCAL, client=(host, 34321))

    def test_checks_physical_host_origin_and_token(self):
        with self.client() as client:
            home = client.get("/")
            self.assertEqual(home.status_code, 200)
            self.assertIn("语音管理", home.text)
            self.assertIn("frame-ancestors 'none'", home.headers["Content-Security-Policy"])
            self.assertEqual(client.get("/api/status").status_code, 401)
            self.assertEqual(client.get("/", headers={"Host": "foreign.test"}).status_code, 403)
            self.assertEqual(client.post(
                "/api/index/actions/deploy", headers={**AUTH, "Origin": "https://foreign.test"}
            ).status_code, 403)
        with self.client(host="198.51.100.1") as client:
            self.assertEqual(client.get("/", headers=AUTH).status_code, 403)

    def test_https_proxy_uses_explicit_host_and_origin(self):
        origin = "https://console.example.test"
        app = create_app(
            self.directory, self.directory / "state", key=KEY,
            storage=self.layout, public_origin=origin,
        )
        # TLS terminates before uvicorn. Forwarded headers alone confer no trust.
        async def fake(*args):
            return 0, b"{}"

        with patch("momo_tts.admin._invoke", fake), TestClient(app, base_url=LOCAL, client=("127.0.0.1", 12345)) as client:
            host = {"Host": "console.example.test"}
            self.assertEqual(client.get("/", headers=host).status_code, 200)
            self.assertEqual(client.get("/api/status", headers=host).status_code, 401)
            self.assertEqual(client.get("/api/status", headers={**host, **AUTH}).status_code, 200)
            self.assertEqual(client.post("/api/index/actions/probe", headers={
                **host, **AUTH, "Origin": "http://console.example.test", "X-Forwarded-Proto": "https",
            }).status_code, 403)
            self.assertEqual(client.post("/api/index/actions/probe", headers={
                **host, **AUTH, "Origin": origin,
            }).status_code, 202)
            self.assertEqual(client.get("/", headers={
                "Host": "foreign.example.test", "X-Forwarded-Host": host["Host"],
            }).status_code, 403)
        with TestClient(create_app(
            self.directory, self.directory / "state", key=KEY,
            storage=self.layout, public_origin=origin,
        ), base_url=origin, client=("198.51.100.1", 12345)) as client:
            self.assertEqual(client.get("/api/status", headers=AUTH).status_code, 403)

    def test_status_allowlist_and_single_job_per_engine(self):
        invoked = []
        entered = threading.Event()
        allow_finish = threading.Event()

        async def fake(source, engine, action, bundle=None):
            if action == "status":
                return 0, json.dumps({
                    "configured": True, "online": True, "revision": "test-version",
                    "reference_configured": True, "api_key": "never exposed", "resident": False, "sessions": 0,
                }).encode()
            invoked.append((engine, action, bundle))
            entered.set()
            await asyncio.to_thread(allow_finish.wait, 3)
            return 0, b"private CLI output"

        with patch("momo_tts.admin._invoke", fake), self.client() as client:
            self.assertEqual(client.post("/api/index/actions/ls", headers=AUTH).status_code, 422)
            self.assertEqual(client.post("/api/unknown/actions/deploy", headers=AUTH).status_code, 422)
            response = client.post("/api/index/actions/deploy", headers={**AUTH, "Origin": LOCAL})
            self.assertEqual(response.status_code, 202)
            self.assertTrue(entered.wait(2))
            self.assertEqual(client.post("/api/index/actions/restart", headers=AUTH).status_code, 409)
            snapshot = client.get("/api/status", headers=AUTH)
            self.assertEqual(snapshot.json()[0]["job"]["state"], "running")
            self.assertNotIn("never exposed", snapshot.text)
            self.assertIs(snapshot.json()[0]["resident"], False)
            self.assertEqual(snapshot.json()[0]["sessions"], 0)
            allow_finish.set()
            for _ in range(20):
                snapshot = client.get("/api/status", headers=AUTH)
                if snapshot.json()[0]["job"]["state"] == "done":
                    break
                time.sleep(.01)
            self.assertEqual(snapshot.json()[0]["job"]["state"], "done")
            self.assertNotIn("private CLI output", snapshot.text)
            self.assertEqual(invoked, [("index", "deploy", None)])

    def test_reference_is_private_and_does_not_turn_text_into_parameters(self):
        completed = threading.Event()
        transcript = "你好，${HOME} 只是本段录音中的台词。"
        source = io.BytesIO()
        with wave.open(source, "wb") as recording:
            recording.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            recording.writeframes(b"\x01\x02" * 32000)
        encoded = base64.b64encode(source.getvalue()).decode()
        observed = {}

        async def fake(root, engine, action, bundle=None):
            metadata = bundle / "reference.json"
            observed.update(
                invocation=(engine, action),
                wav=(bundle / "reference.wav").read_bytes(),
                metadata=json.loads(metadata.read_bytes()),
                permissions=metadata.stat().st_mode & 0o777,
            )
            completed.set()
            return 0, b""

        with patch("momo_tts.admin._invoke", fake), self.client() as client:
            response = client.post("/api/gpt/reference", headers=AUTH, json={"audio": encoded[:-8], "language": "zh", "prompt": transcript})
            self.assertEqual(response.status_code, 422)
            response = client.post("/api/gpt/reference", headers=AUTH, json={"audio": encoded, "language": "zh", "prompt": transcript})
            self.assertEqual(response.status_code, 202)
            self.assertTrue(completed.wait(2))
        self.assertEqual(observed["invocation"], ("gpt", "set-reference"))
        self.assertEqual(observed["wav"], source.getvalue())
        self.assertEqual(observed["metadata"], {"prompt": transcript, "language": "zh"})
        self.assertEqual(observed["permissions"], 0o600)
        self.assertEqual(list((self.directory / "state").iterdir()), [])

    def test_storage_is_global_and_locked_after_configuration(self):
        selected = self.directory / "second disk" / "speech"
        with self.client() as client:
            self.assertEqual(client.get("/api/storage").status_code, 401)
            self.assertEqual(client.get("/api/storage", headers=AUTH).json(), {
                "data_root": str(self.data_root.resolve()), "editable": True,
            })
            self.assertEqual(client.post("/api/storage", headers=AUTH, json={"data_root": "relative"}).status_code, 422)
            response = client.post("/api/storage", headers=AUTH, json={"data_root": str(selected)})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"data_root": str(selected.resolve()), "editable": True})
            service = self.directory / ".config/momo-index/service.env"
            service.parent.mkdir()
            service.touch()
            self.assertEqual(client.post("/api/storage", headers=AUTH, json={
                "data_root": str(self.data_root),
            }).status_code, 409)
            self.assertEqual(client.get("/api/storage", headers=AUTH).json(), {
                "data_root": str(selected.resolve()), "editable": False,
            })

    def test_configuration_only_accepts_gpu_choice(self):
        entered = threading.Event()
        result = {}
        gpu = "GPU-f9cffdb8-1543-a793-36d8-4d8c92e7fae9"

        async def fake(source, engine, action, bundle=None):
            metadata = bundle / "configuration.json"
            result.update(engine=engine, action=action, metadata=json.loads(metadata.read_bytes()),
                          permissions=metadata.stat().st_mode & 0o777)
            entered.set()
            return 0, b""

        with patch("momo_tts.admin._invoke", fake), self.client() as client:
            endpoint = "/api/index/configuration"
            self.assertEqual(client.post(endpoint, headers=AUTH, json={
                "data_dir": str(self.data_root), "gpu": gpu,
            }).status_code, 422)
            self.assertEqual(client.post(endpoint, headers=AUTH, json={"gpu": "GPU-not-a-uuid"}).status_code, 422)
            response = client.post(endpoint, headers=AUTH, json={"gpu": gpu})
            self.assertEqual(response.status_code, 202)
            self.assertTrue(entered.wait(2))
        self.assertEqual(result, {
            "engine": "index", "action": "configure", "metadata": {"gpu": gpu}, "permissions": 0o600,
        })
        self.assertEqual(list((self.directory / "state").iterdir()), [])

    def test_engine_key_requires_explicit_authorized_copy_and_private_regular_file(self):
        home = self.directory / "home"
        directory = home / ".config/momo-gpt"
        directory.mkdir(parents=True)
        (directory / "service.env").touch()
        key_path = directory / "api-key"
        key_path.write_text("a" * 32)
        key_path.chmod(0o600)
        with patch("momo_tts.admin.Path.home", return_value=home), self.client() as client:
            endpoint = "/api/gpt/api-key"
            self.assertEqual(client.post(endpoint).status_code, 401)
            self.assertEqual(client.get(endpoint, headers=AUTH).status_code, 405)
            self.assertEqual(client.post(endpoint, headers={**AUTH, "Origin": "https://foreign.test"}).status_code, 403)
            response = client.post(endpoint, headers=AUTH)
            self.assertEqual(response.json(), {"key": "a" * 32})
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            key_path.chmod(0o640)
            self.assertEqual(client.post(endpoint, headers=AUTH).status_code, 409)
            key_path.rename(directory / "other")
            key_path.symlink_to(directory / "other")
            self.assertEqual(client.post(endpoint, headers=AUTH).status_code, 409)
