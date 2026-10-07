"""Regression guard for pywebview's recursive JS API inspection."""

import unittest
import json
import sys
import tempfile
import threading
import types
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

    def _webview(self):
        webview = types.ModuleType("webview")
        webview.FileDialog = types.SimpleNamespace(OPEN=object())
        return webview

    def _bridge_with_picker(self, chosen, calls=None):
        bridge = DesktopBridge()
        calls = calls if calls is not None else []

        class Window:
            def create_file_dialog(_self, *args, **kwargs):
                calls.append((args, kwargs))
                return chosen() if callable(chosen) else chosen

        bridge._window = Window()
        return bridge, calls

    def test_import_pdf_batch_uses_native_multi_picker_and_continues_after_failure(self):
        chosen = [r"C:\papers\first.pdf", r"C:\papers\broken.pdf", r"C:\papers\duplicate.pdf"]
        bridge, calls = self._bridge_with_picker(chosen)
        jobs = {
            chosen[0]: {"id": "job-1", "message": "已导入源 PDF。"},
            chosen[2]: {"id": "job-existing", "duplicate": True, "message": "已存在相同内容。"},
        }

        def import_one(path, **kwargs):
            if path == chosen[1]:
                raise OSError(r"读取 C:\Users\Alice\secret paper.pdf。")
            self.assertEqual(kwargs, {"mode": "full", "pages": "", "demo_mode": False, "start_translation": False})
            return jobs[path]

        with patch.dict(sys.modules, {"webview": self._webview()}), patch("app.core.import_job_from_path", side_effect=import_one) as importer:
            result = bridge.import_pdf({"mode": "trial", "pages": "1-2", "action": "import"})

        self.assertEqual(result, {
            "results": [
                {"filename": "first.pdf", "job": {"id": "job-1", "duplicate": False, "message": "已导入源 PDF。"}, "duplicate": False},
                {"filename": "broken.pdf", "error": "读取 [本地路径]。"},
                {"filename": "duplicate.pdf", "job": {"id": "job-existing", "duplicate": True, "message": "已存在相同内容。"}, "duplicate": True},
            ],
            "total": 3,
        })
        self.assertEqual(len(importer.call_args_list), 3)
        self.assertEqual(calls[0][1]["allow_multiple"], True)

    def test_import_pdf_single_keeps_legacy_contract(self):
        chosen = [r"C:\papers\single.pdf"]
        bridge, calls = self._bridge_with_picker(chosen)
        job = {"id": "job-1", "duplicate": False, "message": "已加入翻译队列。"}
        with patch.dict(sys.modules, {"webview": self._webview()}), patch("app.core.import_job_from_path", return_value=job) as importer:
            result = bridge.import_pdf({"mode": "trial", "pages": "2-3", "action": "translate"})

        self.assertEqual(result, {"job": job})
        importer.assert_called_once_with(chosen[0], mode="trial", pages="2-3", demo_mode=False, start_translation=True)
        self.assertTrue(calls[0][1]["allow_multiple"])

    def test_import_pdf_validates_options_before_opening_picker(self):
        bridge, calls = self._bridge_with_picker([r"C:\papers\ignored.pdf"])
        with patch.dict(sys.modules, {"webview": self._webview()}), patch("app.core.import_job_from_path") as importer:
            result = bridge.import_pdf({"mode": "unknown"})
        self.assertEqual(result, {"error": "导入选项无效。"})
        self.assertEqual(calls, [])
        importer.assert_not_called()

    def test_import_pdf_cancel_returns_legacy_cancelled_result(self):
        bridge, _calls = self._bridge_with_picker([])
        with patch.dict(sys.modules, {"webview": self._webview()}), patch("app.core.import_job_from_path") as importer:
            result = bridge.import_pdf()
        self.assertEqual(result, {"cancelled": True})
        importer.assert_not_called()

    def test_import_pdf_rejects_multi_translate_before_processing(self):
        bridge, calls = self._bridge_with_picker([r"C:\papers\one.pdf", r"C:\papers\two.pdf"])
        with patch.dict(sys.modules, {"webview": self._webview()}), patch("app.core.import_job_from_path") as importer:
            result = bridge.import_pdf({"action": "demo"})
        self.assertEqual(result, {"error": "批量导入请使用‘仅导入，稍后翻译’，导入并翻译/演示暂仅支持单篇"})
        self.assertEqual(len(calls), 1)
        importer.assert_not_called()

    def test_import_pdf_lock_is_non_blocking_and_released_after_cancel(self):
        entered = threading.Event()
        release = threading.Event()

        def pick_once():
            entered.set()
            release.wait(2)
            return []

        bridge, _calls = self._bridge_with_picker(pick_once)
        with patch.dict(sys.modules, {"webview": self._webview()}):
            thread_result = []
            worker = threading.Thread(target=lambda: thread_result.append(bridge.import_pdf()))
            worker.start()
            self.assertTrue(entered.wait(2))
            self.assertEqual(bridge.import_pdf(), {"error": "已有导入正在进行，请稍候。"})
            release.set()
            worker.join(2)
            self.assertEqual(thread_result, [{"cancelled": True}])
            self.assertEqual(bridge.import_pdf(), {"cancelled": True})


if __name__ == "__main__":
    unittest.main()
