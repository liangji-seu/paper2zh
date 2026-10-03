"""Regression guard for pywebview's recursive JS API inspection."""

import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from desktop import DesktopBridge


class DesktopBridgeTests(unittest.TestCase):
    def test_only_import_method_is_public_to_pywebview(self):
        bridge = DesktopBridge()
        bridge._window = object()
        public = {name for name in dir(bridge) if not name.startswith("_")}
        self.assertEqual(public, {"import_pdf", "save_translation", "copy_markdown_directory", "get_workspace_preferences", "save_workspace_preferences"})

    def _bundle(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        jobs = root / "jobs"
        markdown = jobs / "job-1" / "markdown"
        markdown.mkdir(parents=True)
        (jobs / "job-1" / "job.json").write_text(json.dumps({"id": "job-1", "status": "completed"}), encoding="utf-8")
        (markdown / "source.md").write_text("# source", encoding="utf-8")
        (markdown / "translated.md").write_text("# translated", encoding="utf-8")
        (markdown / "metadata.json").write_text(json.dumps({
            "version": "1", "schema_version": "2", "job_id": "job-1",
            "converter": "PyMuPDF with pypdf fallback", "converter_version": "1",
        }), encoding="utf-8")
        return temporary, jobs, markdown

    def test_copy_markdown_directory_reports_clipboard_failure(self):
        temporary, jobs, _markdown = self._bundle()
        try:
            with patch("app.core.JOBS", jobs), patch("desktop._copy_text_to_windows_clipboard", side_effect=RuntimeError("剪贴板正忙")):
                result = DesktopBridge().copy_markdown_directory("job-1")
            self.assertEqual(result, {"error": "剪贴板正忙"})
        finally:
            temporary.cleanup()

    def test_copy_markdown_directory_returns_verified_directory_after_clipboard_success(self):
        temporary, jobs, markdown = self._bundle()
        try:
            with patch("app.core.JOBS", jobs), patch("desktop._copy_text_to_windows_clipboard") as copy:
                result = DesktopBridge().copy_markdown_directory("job-1")
            self.assertEqual(result, {"copied": True, "directory": str(markdown.resolve())})
            copy.assert_called_once_with(str(markdown.resolve()))
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
