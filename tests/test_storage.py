import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from momo_tts import control


class StorageTest(unittest.TestCase):
    def test_default_project_deploy_starts_console_without_engine(self):
        with patch.dict(os.environ, {"ENGINE": ""}), patch("sys.argv", ["control", "deploy"]), patch(
            "momo_tts.control.install_console"
        ) as install:
            control.main()
            install.assert_called_once_with(reload=True)

    def test_shared_root_allocates_sibling_engine_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            root = home / "scratch" / "speech"
            with patch.object(Path, "home", return_value=home), patch.dict(os.environ, {
                "GPU": "0", "DATA_ROOT": str(root),
            }):
                control.Installation("index").configure()
                control.Installation("gpt").configure()
                self.assertEqual(control.DataLayout().root(), root)
                self.assertEqual(control.Installation("index").settings()["MOMO_TTS_DATA"], str(root / "index-tts"))
                self.assertEqual(control.Installation("gpt").settings()["MOMO_TTS_DATA"], str(root / "gpt-sovits"))
                with self.assertRaisesRegex(RuntimeError, "already configured"):
                    control.DataLayout().select(str(home / "different"))

    def test_existing_console_data_root_remains_the_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            root = home / "ssd"
            with patch.object(Path, "home", return_value=home):
                control.write_environment(home / ".config/momo-tts/console.env", {"MOMO_TTS_ALLOWED_DATA_ROOT": root})
                self.assertEqual(control.DataLayout().root(), root)
