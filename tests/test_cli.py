import contextlib
import io
import unittest
from importlib.metadata import PackageNotFoundError
from unittest.mock import Mock, patch

from video_app.cli import main


class CliTests(unittest.TestCase):
    def test_help_exits_without_starting_gui(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("openrouter-video", output.getvalue())

    def test_version_uses_package_metadata(self):
        output = io.StringIO()
        with patch("video_app.cli.version", return_value="0.1.0"), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), "openrouter-video 0.1.0")

    def test_uninstalled_source_version(self):
        output = io.StringIO()
        with patch("video_app.cli.version", side_effect=PackageNotFoundError), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("development", output.getvalue())

    def test_default_command_starts_gui(self):
        gui = Mock()
        gui.run.return_value = 0
        with patch.dict("sys.modules", {"video_app.gui": gui}):
            self.assertEqual(main([]), 0)
        gui.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
