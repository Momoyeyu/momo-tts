"""Host operations shared by Make and the loopback management console."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from .probe import load_reference, make_reference, remote_status, request, synthesize, wait_ready
from .protocol import ENGINES, admin_origin, read_key
from .network import CONSOLE_PORT, console_address

SOURCE = Path(__file__).resolve().parents[1]
LABELS = {"index": "IndexTTS", "gpt": "GPT-SoVITS"}
RELEASE = re.compile(r"[0-9a-f]{40}")


def atomic_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(content)
            file.flush()
            os.fchmod(file.fileno(), mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def read_text(path):
    return path.read_text().strip() if path.is_file() else ""


def installation_environment(path):
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        fields = shlex.split(value)
        if len(fields) != 1:
            raise RuntimeError("Invalid installation configuration")
        values[key] = fields[0]
    return values


def write_environment(path, settings):
    # Values are literal for systemd and this module. No shell sources the file.
    atomic_write(path, "".join(f"{key}={shlex.quote(str(value))}\n" for key, value in settings.items()))



class DataLayout:
    engine_directories = {"index": "index-tts", "gpt": "gpt-sovits"}

    def __init__(self, home=None):
        self.home = Path.home() if home is None else home
        self.config = self.home / ".config/momo-tts"
        self.root_file = self.config / "data-root"

    def root(self):
        selected = read_text(self.root_file)
        legacy = self.config / "console.env"
        if not selected and legacy.is_file():
            selected = installation_environment(legacy).get("MOMO_TTS_ALLOWED_DATA_ROOT")
        return Path(selected or self.home / ".local/share/momo-tts-data").resolve()

    def editable(self):
        return not any(
            (self.home / f".config/momo-{name}/service.env").is_file()
            for name in self.engine_directories
        )

    @contextmanager
    def exclusive(self):
        self.config.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.config / "storage.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def select(self, value=None):
        with self.exclusive():
            return self.select_locked(value)

    def select_locked(self, value):
        current = self.root()
        candidate = Path(value) if value else current
        if not candidate.is_absolute() or any(not char.isprintable() or char == "'" for char in str(candidate)):
            raise ValueError("DATA_ROOT must be a simple absolute path")
        candidate = candidate.resolve()
        if candidate == Path(candidate.anchor):
            raise ValueError("DATA_ROOT must be a directory below the filesystem root")
        if candidate != current and not self.editable():
            raise RuntimeError("The data root is already configured for an engine")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not os.access(candidate, os.W_OK | os.X_OK):
            raise PermissionError("The data root must be writable by the service account")
        if candidate != current or not self.root_file.is_file():
            atomic_write(self.root_file, str(candidate) + "\n")
        return candidate

def run(*args, **kwargs):
    return subprocess.run(args, cwd=SOURCE, check=True, **kwargs)


def systemctl(*args, **kwargs):
    return run("systemctl", "--user", *args, **kwargs)


def active(service):
    return subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", service],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


class Installation:
    def __init__(self, name):
        self.name = name
        self.endpoint = ENGINES[name]
        self.state = Path.home() / f".local/share/momo-{name}"
        self.config = Path.home() / f".config/momo-{name}"
        self.env_path = self.config / "service.env"
        self.key_file = self.config / "api-key"
        self.units = Path.home() / ".config/systemd/user"
        self.gateway = f"momo-{name}-gateway.service"

    def worker(self, slot):
        return f"momo-{self.name}-worker@{slot}.service"

    @contextmanager
    def exclusive(self):
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / "deployment.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another operation is running for this engine") from None
            yield

    def settings(self):
        if not self.env_path.is_file():
            raise RuntimeError("Run make configure first")
        values = installation_environment(self.env_path)
        data = values.get("MOMO_TTS_DATA") or values["MOMO_INDEX_DATA"]
        key = values.get("MOMO_TTS_KEY_FILE") or values["MOMO_INDEX_KEY_FILE"]
        values.update(MOMO_TTS_ENGINE=self.name, MOMO_TTS_STATE=str(self.state),
                      MOMO_TTS_DATA=data, MOMO_TTS_KEY_FILE=key)
        return values

    def configure(self):
        layout = DataLayout()
        with layout.exclusive():
            self._configure(layout)

    def _configure(self, layout):
        if os.environ.get("DATA_DIR"):
            raise RuntimeError("Use DATA_ROOT for both engines")
        root = layout.select_locked(os.environ.get("DATA_ROOT"))
        bundle = os.environ.get("MOMO_TTS_CONFIGURATION_BUNDLE")
        gpu = json.loads(Path(bundle).read_text())["gpu"] if bundle else os.environ.get("GPU", "")
        if self.env_path.exists():
            settings = self.settings()
            if gpu and gpu != settings["CUDA_VISIBLE_DEVICES"]:
                raise RuntimeError("Engine is already configured with a different GPU")
            print(f"{LABELS[self.name]} already configured")
            return
        data = root / layout.engine_directories[self.name]
        if data.is_symlink():
            raise RuntimeError("The engine data directory must not be a symbolic link")
        if not re.fullmatch(r"\d+|GPU-[0-9a-fA-F-]+", gpu):
            raise RuntimeError("GPU must be an index or the UUID shown by nvidia-smi -L")
        data.mkdir(parents=True, exist_ok=True)
        for directory in ("cache", "tmp", "smoke"):
            (data / directory).mkdir(exist_ok=True)
        if not self.key_file.exists():
            atomic_write(self.key_file, secrets.token_urlsafe(32) + "\n")
        read_key(self.key_file)
        settings = {
            "MOMO_TTS_ENGINE": self.name, "MOMO_TTS_STATE": self.state,
            "MOMO_TTS_DATA": data, "MOMO_TTS_KEY_FILE": self.key_file,
            "CUDA_VISIBLE_DEVICES": gpu, "XDG_CACHE_HOME": data / "cache", "TMPDIR": data / "tmp",
            "HF_HOME": data / "cache/huggingface", "MODELSCOPE_CACHE": data / "cache/modelscope",
            "UV_CACHE_DIR": data / "cache/uv", "UV_PYTHON_INSTALL_DIR": data / "python",
        }
        if self.name == "index":
            settings.update(MOMO_INDEX_MODELS=data / "checkpoints", HF_HUB_CACHE=data / "checkpoints/hf_cache",
                            UV_PROJECT_ENVIRONMENT=data / "venv")
        write_environment(self.env_path, settings)
        print(f"{LABELS[self.name]} configured")

    def prepare(self, settings):
        if any(active(self.worker(slot)) for slot in self.endpoint.slots):
            raise RuntimeError("The inference environment is in use. Preparation needs a maintenance window.")
        environment = dict(os.environ, **settings)
        environment.pop("HF_HUB_OFFLINE", None)
        environment.pop("TRANSFORMERS_OFFLINE", None)
        environment["PATH"] = str(Path.home() / ".local/bin") + ":" + environment["PATH"]
        script = "scripts/prepare-index" if self.name == "index" else "gpt-sovits/prepare"
        run(str(SOURCE / script), env=environment)

    def bootstrap(self, settings):
        python_path = self.state / "gateway-env/bin/python"
        environment = dict(os.environ, **settings)
        environment["PATH"] = str(Path.home() / ".local/bin") + ":" + environment["PATH"]
        if not python_path.is_file():
            run("uv", "venv", "--allow-existing", "--python", "/usr/bin/python3",
                str(python_path.parent.parent), env=environment)
            run("uv", "pip", "install", "--python", str(python_path), "-r", "requirements.txt", env=environment)
        gateway_code = self.state / "gateway/momo_tts"
        upgrade_legacy = gateway_code.is_dir() and not (gateway_code / "residency.py").is_file()
        for filename in ("__init__.py", "api.py", "gateway.py", "protocol.py", "residency.py"):
            atomic_write(gateway_code / filename, (SOURCE / "momo_tts" / filename).read_text())
        atomic_write(self.state / "run", (SOURCE / "systemd/run").read_text(), 0o700)
        (self.state / "slots").mkdir(exist_ok=True)
        for slot in self.endpoint.slots:
            (self.state / f"{slot}.lock").touch()
        for template, filename in (("gateway", self.gateway), ("worker", self.worker(""))):
            text = (SOURCE / f"systemd/{template}.service.in").read_text()
            text = text.replace("@ENGINE@", self.name).replace("@LABEL@", LABELS[self.name]).replace("@PORT@", self.endpoint.port)
            atomic_write(self.units / filename, text, 0o644)
        systemctl("daemon-reload")
        for slot in self.endpoint.slots:
            systemctl("disable", self.worker(slot))
        systemctl("enable", "--now", self.gateway)
        if upgrade_legacy:
            systemctl("restart", self.gateway)

    def active_slot(self):
        value = read_text(self.state / "active")
        if value and value not in self.endpoint.slots:
            raise RuntimeError("Active worker pointer is invalid")
        return value

    def revision(self, slot):
        link = self.state / "slots" / slot
        return link.resolve().name if link.is_symlink() else ""

    def stage(self, revision):
        if not RELEASE.fullmatch(revision):
            raise RuntimeError("Invalid release revision")
        releases = self.state / "releases"
        releases.mkdir(exist_ok=True)
        destination = releases / revision
        if not destination.is_dir():
            with tempfile.TemporaryDirectory(dir=releases) as temp:
                archive = Path(temp) / "source.tar"
                run("git", "archive", "--output", str(archive), revision)
                extract = Path(temp) / "source"
                extract.mkdir()
                run("tar", "-xf", str(archive), "-C", str(extract))
                os.replace(extract, destination)
        return destination

    def point(self, slot, target):
        path = self.state / "slots" / slot
        temporary = path.with_suffix(".new")
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(target)
        os.replace(temporary, path)

    def retire(self, slot):
        run("flock", "-w", "135", str(self.state / f"{slot}.lock"),
            "systemctl", "--user", "disable", "--now", self.worker(slot))

    def switch(self, action, settings):
        if not active(self.gateway):
            raise RuntimeError("Run make bootstrap first")
        previous = self.active_slot()
        candidate = next(slot for slot in self.endpoint.slots if slot != previous)
        current_revision = self.revision(previous)
        kept = self.revision(candidate)
        if action == "restart":
            release = current_revision
        elif action == "rollback":
            release = read_text(self.state / "previous-release") or kept
            if release == current_revision:
                raise RuntimeError("There is no different release to roll back to")
        else:
            release = run("git", "rev-parse", "HEAD", capture_output=True, text=True).stdout.strip()
        if not release:
            raise RuntimeError("There is no release yet. Run make deploy first.")
        data = Path(settings["MOMO_TTS_DATA"])
        reference = load_reference(data, self.name)
        key = read_key(Path(settings["MOMO_TTS_KEY_FILE"]))
        target = self.stage(release)
        self.retire(candidate)
        self.point(candidate, target)
        try:
            subprocess.run(["systemctl", "--user", "reset-failed", self.worker(candidate)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            systemctl("start", self.worker(candidate))
            wait_ready(candidate, key, 600)
            synthesize(self.name, candidate, key, reference)
            atomic_write(self.state / "active", candidate + "\n")
        except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
            try:
                self.retire(candidate)
            except subprocess.CalledProcessError:
                pass
            if kept:
                self.point(candidate, self.state / "releases" / kept)
            else:
                (self.state / "slots" / candidate).unlink(missing_ok=True)
            raise RuntimeError(f"Candidate deployment failed. Live endpoint unchanged. {error}") from error
        try:
            if current_revision and current_revision != release:
                atomic_write(self.state / "previous-release", current_revision + "\n")
        finally:
            if previous:
                try:
                    self.retire(previous)
                except subprocess.CalledProcessError:
                    print(f"Traffic switched. Slot {previous} still needs retirement.", file=sys.stderr)
        print(f"{LABELS[self.name]} active on slot {candidate} ({release[:12]})")

    def set_reference(self, settings):
        bundle = os.environ.get("MOMO_TTS_REFERENCE_BUNDLE")
        if bundle:
            directory = Path(bundle)
            metadata = json.loads((directory / "reference.json").read_text())
            path, prompt, language = directory / "reference.wav", metadata["prompt"], metadata["language"]
        else:
            path = Path(os.environ.get("REFERENCE", ""))
            prompt = os.environ.get("PROMPT", "")
            language = os.environ.get("LANGUAGE") or "zh"
        reference = make_reference(path, prompt, language)
        if self.name == "gpt" and not reference["prompt"]:
            raise RuntimeError("GPT-SoVITS needs the exact reference transcript")
        atomic_write(Path(settings["MOMO_TTS_DATA"]) / "smoke/reference.json", json.dumps(reference))
        print("Validation reference saved")

    def show_status(self):
        state = {"engine": self.name, "online": False, "configured": self.env_path.is_file(),
                 "active_slot": None, "revision": None, "gpu": None, "data_dir": None,
                 "reference_configured": False, "resident": False, "sessions": 0, "gateway_online": False}
        if state["configured"]:
            settings = self.settings()
            data = Path(settings["MOMO_TTS_DATA"])
            reference = (data / "smoke/reference.json").is_file() or (
                self.name == "index" and (data / "smoke/reference.wav").is_file()
            )
            slot = self.active_slot()
            health = remote_status(self.endpoint.port, read_key(Path(settings["MOMO_TTS_KEY_FILE"])))
            state.update(gpu=settings["CUDA_VISIBLE_DEVICES"], data_dir=str(data),
                         active_slot=slot or None, revision=self.revision(slot) or None,
                         reference_configured=reference, online=bool(slot and health.get("ready")),
                         gateway_online=bool(health) or active(self.gateway),
                         resident=health.get("resident") is True, sessions=health.get("sessions", 0))
        print(json.dumps(state))


def install_console(*, reload=False):
    config = Path.home() / ".config/momo-tts"
    state = Path.home() / ".local/share/momo-tts"
    env_path = config / "console.env"
    previous = installation_environment(env_path) if env_path.is_file() else {}
    origin = admin_origin(os.environ.get("ADMIN_ORIGIN") or previous.get("MOMO_TTS_ADMIN_ORIGIN", ""))
    requested_binding = os.environ.get("ADMIN_BIND")
    binding = requested_binding or previous.get("MOMO_TTS_ADMIN_BIND") or "auto"
    saved_address = previous.get("MOMO_TTS_ADMIN_LAN") if not requested_binding and binding == "auto" else ""
    lan = console_address(saved_address or binding, os.environ.get("SSH_CONNECTION", ""))
    root = DataLayout().select(os.environ.get("DATA_ROOT"))
    python_path = state / "console-env/bin/python"
    if not python_path.is_file():
        environment = dict(os.environ, UV_CACHE_DIR=str(root / "manager-cache"))
        environment["PATH"] = str(Path.home() / ".local/bin") + ":" + environment["PATH"]
        run("uv", "venv", "--allow-existing", "--python", "/usr/bin/python3",
            str(python_path.parent.parent), env=environment)
        run("uv", "pip", "install", "--python", str(python_path), "-r", "requirements.txt", env=environment)
    key = config / "admin-key"
    if not key.exists():
        atomic_write(key, secrets.token_urlsafe(32) + "\n")
    jobs = state / "console"
    jobs.mkdir(parents=True, exist_ok=True, mode=0o700)
    jobs.chmod(0o700)
    console_key = read_key(key)
    settings = {
        "MOMO_TTS_ADMIN_KEY_FILE": key, "MOMO_TTS_SOURCE": SOURCE, "MOMO_TTS_ADMIN_STATE": jobs,
        "MOMO_TTS_ADMIN_PYTHON": python_path, "MOMO_TTS_ALLOWED_DATA_ROOT": root,
        "MOMO_TTS_ADMIN_BIND": binding, "MOMO_TTS_ADMIN_LAN": lan,
    }
    if origin:
        settings["MOMO_TTS_ADMIN_ORIGIN"] = origin
    write_environment(env_path, settings)
    atomic_write(state / "console-run", (SOURCE / "systemd/console-run").read_text(), 0o700)
    unit = "momo-tts-console.service"
    atomic_write(Path.home() / ".config/systemd/user" / unit, (SOURCE / "systemd" / unit).read_text(), 0o644)
    systemctl("daemon-reload")
    running = active(unit)
    systemctl("enable", "--now", unit)
    if running and (reload or {field: str(value) for field, value in settings.items()} != previous):
        systemctl("restart", unit)
    deadline = time.monotonic() + 10
    while True:
        try:
            with request(CONSOLE_PORT, "/", console_key, timeout=1):
                break
        except OSError as error:
            if time.monotonic() >= deadline:
                raise RuntimeError("Management console did not become reachable on port 19890") from error
            time.sleep(.25)
    if lan:
        print(f"LAN console: http://{lan}:{CONSOLE_PORT} (HTTP)")
    if origin:
        print(f"HTTPS console: {origin}")
    if not lan and not origin:
        print(f"Local console: http://127.0.0.1:{CONSOLE_PORT}")
    print(f"Admin key file: {key}")


def main():
    os.umask(0o077)
    action = sys.argv[1]
    engine = os.environ.get("ENGINE")
    name = engine or "index"
    try:
        if action in ("console", "install") or (action == "deploy" and not engine):
            install_console(reload=action == "deploy")
            return
        if name not in ENGINES:
            raise RuntimeError("Use ENGINE=index or ENGINE=gpt")
        installation = Installation(name)
        if action == "status":
            installation.show_status()
        elif action == "probe":
            settings = installation.settings()
            port = installation.endpoint.port
            key = read_key(Path(settings["MOMO_TTS_KEY_FILE"]))
            wait_ready(port, key)
            reference = load_reference(Path(settings["MOMO_TTS_DATA"]), name)
            synthesize(name, port, key, reference, os.environ.get("OUTPUT_LANGUAGE") or "zh")
        else:
            with installation.exclusive():
                if action == "configure":
                    installation.configure()
                    return
                settings = installation.settings()
                if action in ("deploy", "restart", "rollback"):
                    installation.switch(action, settings)
                else:
                    command = {"prepare": installation.prepare, "bootstrap": installation.bootstrap,
                               "set-reference": installation.set_reference}
                    command[action](settings)
    except (RuntimeError, OSError, subprocess.CalledProcessError, KeyError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
