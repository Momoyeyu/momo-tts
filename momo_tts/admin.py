"""Operator console for both engines, served through a local listener."""

import asyncio
import base64
import binascii
from contextlib import asynccontextmanager
import csv
import hmac
import io
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import stat
import subprocess
import tempfile
from typing import Literal
from urllib.parse import urlsplit
import wave

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from .api import MAX_REFERENCE, read_body
from .protocol import admin_origin

Engine = Literal["index", "gpt"]
Operation = Literal["bootstrap", "prepare", "deploy", "restart", "rollback", "probe"]
STATIC = Path(__file__).with_name("admin_static")
LOOPBACK = {"127.0.0.1", "::1"}
HOSTS = LOOPBACK | {"localhost"}
STATUS_FIELDS = (
    "online", "configured", "active_slot", "revision", "gpu", "data_dir", "reference_configured",
    "resident", "sessions",
)
GPU_UUID = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
COMMAND_TIMEOUT = {"status": 12, "configure": 30, "prepare": 7200, "probe": 120, "set-reference": 30}
SECURITY = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "form-action 'none'; frame-ancestors 'none'; base-uri 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _private_key(path: Path) -> bytes:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as key_file:
        info = os.fstat(key_file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise RuntimeError("The key file must be private")
        key = key_file.read(258).strip()
    if not 32 <= len(key) <= 256 or any(not 33 <= byte <= 126 for byte in key):
        raise RuntimeError("Invalid key format")
    return key


def _read_key() -> bytes:
    return _private_key(Path(os.environ["MOMO_TTS_ADMIN_KEY_FILE"]))


async def _invoke(source: Path, engine: str, action: str, bundle: Path | None = None):
    capture = action == "status"
    # Keep the service environment needed by systemd and the configured dependencies.
    # Only the server-generated bundle path is allowed to vary with a browser request.
    env = os.environ.copy()
    for name in (
        "MOMO_TTS_ADMIN_KEY_FILE", "MOMO_TTS_REFERENCE_BUNDLE", "MOMO_TTS_CONFIGURATION_BUNDLE",
        "MAKEFLAGS", "MFLAGS", "MAKEFILES",
    ):
        env.pop(name, None)
    if bundle is not None:
        if action == "configure":
            env["MOMO_TTS_CONFIGURATION_BUNDLE"] = str(bundle / "configuration.json")
        else:
            env["MOMO_TTS_REFERENCE_BUNDLE"] = str(bundle)
    process = await asyncio.create_subprocess_exec(
        "make", "--silent", "--no-print-directory", "-C", str(source), action, f"ENGINE={engine}",
        env=env, start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(COMMAND_TIMEOUT.get(action, 900)):
            if capture:
                chunks = bytearray()
                while chunk := await process.stdout.read(4096):
                    chunks.extend(chunk)
                    if len(chunks) > 8192:
                        raise ValueError("Unexpected status size")
                output = bytes(chunks)
            else:
                output = b""
            code = await process.wait()
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
    return code, output


def _reference(raw: bytes) -> tuple[bytes, str, str]:
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        audio = base64.b64decode(data["audio"], validate=True)
        prompt = data["prompt"].strip()
        language = data["language"]
        if not 0 < len(audio) <= MAX_REFERENCE or not 0 < len(prompt) <= 500 or language not in ("zh", "en", "ja"):
            raise ValueError
        with wave.open(io.BytesIO(audio)) as wav:
            count = wav.getnframes()
            rate = wav.getframerate()
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            if not 8_000 <= rate <= 192_000 or channels != 1 or width != 2 or not 3 <= count / rate <= 10:
                raise ValueError
            if len(wav.readframes(count)) != count * 2:
                raise ValueError
        return audio, prompt, language
    except (AttributeError, TypeError, KeyError, ValueError, binascii.Error, EOFError, wave.Error) as exc:
        raise HTTPException(422, "参考音需要 3–10 秒的单声道 PCM16 WAV、对应台词及语言。") from exc


async def _configuration(request: Request, root: Path) -> dict[str, str]:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 4096:
            raise HTTPException(413, "配置内容过长。")
        body.extend(chunk)
    try:
        data = json.loads(body)
        path, gpu = data["data_dir"], data["gpu"]
        if not isinstance(path, str) or not isinstance(gpu, str):
            raise ValueError
        if any(not char.isprintable() or char == "'" for char in path):
            raise ValueError
        directory = Path(path)
        if not directory.is_absolute() or not GPU_UUID.fullmatch(gpu):
            raise ValueError
        directory = directory.resolve()
    except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(422, "请填写绝对数据目录和有效的 GPU UUID。") from exc
    if root not in directory.parents or (directory.exists() and not directory.is_dir()):
        raise HTTPException(422, "请选择允许的数据目录下的子目录。")
    return {"data_dir": str(directory), "gpu": gpu}


def _gpu_choices():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid,name", "--format=csv,noheader"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=4, check=True, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    choices = []
    for row in csv.reader(result.stdout.splitlines(), skipinitialspace=True):
        if len(row) == 3 and row[0].isdigit() and GPU_UUID.fullmatch(row[1]):
            choices.append({"index": int(row[0]), "uuid": row[1], "name": row[2]})
    return choices


def create_app(source=None, state_dir=None, key=None, allowed_data_root=None, public_origin=None):
    external_origin = admin_origin(public_origin if public_origin is not None else os.environ.get("MOMO_TTS_ADMIN_ORIGIN", ""))
    external_host = urlsplit(external_origin).netloc
    @asynccontextmanager
    async def lifespan(app):
        app.state.source = Path(source or os.environ["MOMO_TTS_SOURCE"]).resolve(strict=True)
        if not app.state.source.is_dir():
            raise RuntimeError("The admin source must be a directory")
        app.state.directory = Path(state_dir or os.environ["MOMO_TTS_ADMIN_STATE"]).resolve()
        app.state.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if app.state.directory.stat().st_mode & 0o077:
            raise RuntimeError("The admin state directory must be private")
        app.state.key = key if key is not None else _read_key()
        default_root = Path("/data") / pwd.getpwuid(os.getuid()).pw_name / "momo-tts"
        app.state.allowed_data_root = Path(allowed_data_root or os.environ.get(
            "MOMO_TTS_ALLOWED_DATA_ROOT", default_root
        )).resolve()
        yield
        tasks = list(active_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    active_tasks: dict[str, asyncio.Task] = {}
    jobs: dict[str, dict[str, str]] = {}

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        scheme = request.url.scheme
        host = request.headers.get("host", "")
        try:
            hostname = urlsplit(f"{scheme}://{host}").hostname
        except ValueError:
            hostname = None
        origin = request.headers.get("origin")
        external = bool(external_host and host.lower() == external_host)
        if not request.client or request.client.host not in LOOPBACK or not (hostname in HOSTS or external):
            return PlainTextResponse("Console host not allowed", status_code=403, headers=SECURITY)
        expected_origin = external_origin if external else f"{scheme}://{host}"
        if request.method == "POST" and origin is not None and origin != expected_origin:
            return PlainTextResponse("Origin mismatch", status_code=403, headers=SECURITY)
        response = await call_next(request)
        response.headers.update(SECURITY)
        return response

    def authorize(request: Request):
        provided = request.headers.get("authorization", "").encode("latin-1")
        if not hmac.compare_digest(provided, b"Bearer " + app.state.key):
            raise HTTPException(401, "管理密钥有误。")

    async def run(engine: str, action: str, job: dict[str, str], bundle: Path | None):
        try:
            code, _ = await _invoke(app.state.source, engine, action, bundle)
            job["state"] = "done" if code == 0 else "failed"
        except (OSError, ValueError, TimeoutError):
            job["state"] = "failed"
        finally:
            if bundle is not None:
                shutil.rmtree(bundle)

    def ensure_idle(engine: str):
        if engine in active_tasks and not active_tasks[engine].done():
            raise HTTPException(409, "此引擎正在执行操作。")

    def start(engine: str, action: str, bundle: Path | None = None):
        job = {"action": action, "state": "running"}
        jobs[engine] = job
        active_tasks[engine] = asyncio.create_task(run(engine, action, job, bundle))
        return job

    async def status(engine: str):
        result = {"engine": engine, "job": jobs.get(engine),
                  "allowed_data_root": str(app.state.allowed_data_root)}
        try:
            code, output = await _invoke(app.state.source, engine, "status")
            details = json.loads(output)
            if code or not isinstance(details, dict):
                raise ValueError
            result.update({field: details.get(field) for field in STATUS_FIELDS})
        except (OSError, ValueError, TimeoutError):
            result["status_unavailable"] = True
        return result

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/admin.css")
    async def style():
        return FileResponse(STATIC / "admin.css", media_type="text/css")

    @app.get("/admin.js")
    async def script():
        return FileResponse(STATIC / "admin.js", media_type="text/javascript")

    @app.get("/api/status", dependencies=[Depends(authorize)])
    async def get_status():
        return await asyncio.gather(status("index"), status("gpt"))

    @app.post("/api/{engine}/actions/{action}", dependencies=[Depends(authorize)], status_code=202)
    async def action(engine: Engine, action: Operation):
        ensure_idle(engine)
        return start(engine, action)

    @app.get("/api/gpus", dependencies=[Depends(authorize)])
    async def get_gpus():
        return await asyncio.to_thread(_gpu_choices)

    @app.post("/api/{engine}/api-key", dependencies=[Depends(authorize)])
    async def engine_key(engine: Engine):
        directory = Path.home() / ".config" / ("momo-index" if engine == "index" else "momo-gpt")
        if not (directory / "service.env").is_file():
            raise HTTPException(404, "该引擎尚未配置。")
        try:
            return {"key": _private_key(directory / "api-key").decode("ascii")}
        except (OSError, RuntimeError) as exc:
            raise HTTPException(409, "无法读取此引擎的密钥。") from exc

    @app.post("/api/{engine}/configuration", dependencies=[Depends(authorize)], status_code=202)
    async def configure(engine: Engine, request: Request):
        ensure_idle(engine)
        configuration = await _configuration(request, app.state.allowed_data_root)
        ensure_idle(engine)
        bundle = Path(tempfile.mkdtemp(prefix="configuration-", dir=app.state.directory))
        try:
            metadata = bundle / "configuration.json"
            metadata.touch(mode=0o600, exist_ok=False)
            metadata.write_text(json.dumps(configuration, ensure_ascii=False), encoding="utf-8")
            return start(engine, "configure", bundle)
        except BaseException:
            shutil.rmtree(bundle)
            raise

    @app.post("/api/{engine}/reference", dependencies=[Depends(authorize)], status_code=202)
    async def reference(engine: Engine, request: Request):
        ensure_idle(engine)
        audio, prompt, language = _reference(await read_body(request))
        ensure_idle(engine)
        bundle = Path(tempfile.mkdtemp(prefix="reference-", dir=app.state.directory))
        try:
            wav, metadata = bundle / "reference.wav", bundle / "reference.json"
            wav.touch(mode=0o600, exist_ok=False)
            metadata.touch(mode=0o600, exist_ok=False)
            wav.write_bytes(audio)
            metadata.write_text(
                json.dumps({"prompt": prompt, "language": language}, ensure_ascii=False), encoding="utf-8"
            )
            return start(engine, "set-reference", bundle)
        except BaseException:
            shutil.rmtree(bundle)
            raise

    return app
