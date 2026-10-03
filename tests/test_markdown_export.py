from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import markdown_export


class MarkdownExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.jobs = self.data / "jobs"
        self.job_dir = self.jobs / "job-1"
        (self.job_dir / "translate").mkdir(parents=True)
        runtime = Path(__file__).resolve().parents[1] / ".runtime311"
        if runtime.is_dir() and str(runtime) not in sys.path:
            sys.path.insert(0, str(runtime))
        try:
            import fitz
        except ImportError as exc:  # pragma: no cover - requirements include PyMuPDF
            self.skipTest(f"PyMuPDF unavailable: {exc}")
        self.fitz = fitz
        self._pdf(self.job_dir / "source.pdf", ["Source page one", "Source page two"])
        self._pdf(self.job_dir / "translate" / "paper.zh.pdf", ["Translated page one", "Translated page two"])

    def tearDown(self):
        self.tmp.cleanup()

    def _pdf(self, path: Path, texts: list[str]):
        document = self.fitz.open()
        for text in texts:
            page = document.new_page()
            if text:
                page.insert_text((50, 70), text, fontsize=12)
        document.save(path)
        document.close()

    def _job(self, **changes):
        job = {
            "id": "job-1",
            "filename": "paper.pdf",
            "display_title": "论文原名",
            "translated_title": "论文中文名",
            "status": "completed",
            "mode": "full",
            "translation_scope": "full",
            "pages": "",
            "page_count": 2,
            "translated_file": "translate/paper.zh.pdf",
        }
        job.update(changes)
        return job

    def test_source_and_translated_markdown_have_page_links_and_metadata(self):
        result = markdown_export.export_job(self.data, self.jobs, self._job())
        markdown = self.job_dir / "markdown"
        source = (markdown / "source.md").read_text(encoding="utf-8")
        translated = (markdown / "translated.md").read_text(encoding="utf-8")
        metadata = json.loads((markdown / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "exported")
        self.assertIn("Source page one", source)
        self.assertIn("source-page-1", source)
        self.assertIn("../source.pdf#page=2", source)
        self.assertIn("Translated page two", translated)
        self.assertIn("../translate/paper.zh.pdf#page=1", translated)
        self.assertEqual(metadata["job_id"], "job-1")
        self.assertEqual(metadata["title"], "论文中文名")
        self.assertEqual(metadata["selected_pages"], [1, 2])
        self.assertEqual(metadata["scope"], "full")
        self.assertRegex(metadata["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn(str(self.data), json.dumps(metadata, ensure_ascii=False))
        index = json.loads((self.data / "index.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["id"] for entry in index["entries"]], ["job-1"])
        self.assertEqual(index["entries"][0]["source"], "jobs/job-1/markdown/source.md")

    def test_markdown_directory_requires_a_complete_owned_bundle(self):
        markdown_export.export_job(self.data, self.jobs, self._job())
        directory = markdown_export.markdown_directory(self.jobs, "job-1", self._job())
        self.assertEqual(directory, (self.job_dir / "markdown").resolve())
        with self.assertRaisesRegex(markdown_export.MarkdownExportError, "不安全"):
            markdown_export.markdown_directory(self.jobs, "../outside", self._job(id="../outside"))

        incomplete = self.jobs / "job-2"
        incomplete.mkdir()
        (incomplete / "job.json").write_text(json.dumps(self._job(id="job-2")), encoding="utf-8")
        with self.assertRaisesRegex(markdown_export.MarkdownExportError, "尚未"):
            markdown_export.markdown_directory(self.jobs, "job-2")

    def test_markdown_directory_rejects_symlink_escape(self):
        markdown_export.export_job(self.data, self.jobs, self._job())
        outside = self.data / "outside"
        outside.mkdir()
        escaped = outside / "source.md"
        escaped.write_text("outside", encoding="utf-8")
        source = self.job_dir / "markdown" / "source.md"
        source.unlink()
        try:
            source.symlink_to(escaped)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(markdown_export.MarkdownExportError):
            markdown_export.markdown_directory(self.jobs, "job-1", self._job())

    def test_trial_keeps_full_source_and_limits_translated_pages(self):
        result = markdown_export.export_job(self.data, self.jobs, self._job(mode="trial", translation_scope="trial", pages="2"))
        source = (self.job_dir / "markdown" / "source.md").read_text(encoding="utf-8")
        metadata = result["metadata"]
        self.assertEqual(metadata["scope"], "trial")
        self.assertEqual(metadata["selected_pages"], [2])
        self.assertIn("Source page one", source)
        self.assertIn("Source page two", source)
        translated = (self.job_dir / "markdown" / "translated.md").read_text(encoding="utf-8")
        self.assertNotIn("Translated page one", translated)
        self.assertIn("Translated page two", translated)

    def test_empty_scanned_page_is_explicitly_marked(self):
        self._pdf(self.job_dir / "source.pdf", [""])
        self._pdf(self.job_dir / "translate" / "paper.zh.pdf", [""])
        result = markdown_export.export_job(self.data, self.jobs, self._job(page_count=1))
        self.assertEqual(result["metadata"]["selected_pages"], [1])
        source = (self.job_dir / "markdown" / "source.md").read_text(encoding="utf-8")
        translated = (self.job_dir / "markdown" / "translated.md").read_text(encoding="utf-8")
        self.assertEqual(source.count("正文提取不可用"), 1)
        self.assertEqual(translated.count("正文提取不可用"), 1)

    def test_same_input_reuses_bundle_without_overwriting_manual_markdown(self):
        markdown_export.export_job(self.data, self.jobs, self._job())
        source_path = self.job_dir / "markdown" / "source.md"
        source_path.write_text("manual note\n", encoding="utf-8")
        result = markdown_export.export_job(self.data, self.jobs, self._job())
        self.assertEqual(result["status"], "reused")
        self.assertEqual(source_path.read_text(encoding="utf-8"), "manual note\n")

    def test_converter_version_change_invalidates_cache(self):
        markdown_export.export_job(self.data, self.jobs, self._job())
        source_path = self.job_dir / "markdown" / "source.md"
        source_path.write_text("old converter output\n", encoding="utf-8")
        with patch.object(markdown_export, "CONVERTER_VERSION", "2"):
            result = markdown_export.export_job(self.data, self.jobs, self._job())
        self.assertEqual(result["status"], "exported")
        self.assertNotEqual(source_path.read_text(encoding="utf-8"), "old converter output\n")

    def test_failed_rebuild_keeps_previous_bundle(self):
        markdown_export.export_job(self.data, self.jobs, self._job())
        source_path = self.job_dir / "markdown" / "source.md"
        source_path.write_text("keep this old export\n", encoding="utf-8")
        self._pdf(self.job_dir / "source.pdf", ["changed source"])
        with patch.object(markdown_export, "_extract_pages", side_effect=RuntimeError("extract failed")):
            with self.assertRaises(RuntimeError):
                markdown_export.export_job(self.data, self.jobs, self._job())
        self.assertEqual(source_path.read_text(encoding="utf-8"), "keep this old export\n")

    def test_queue_deduplicates_same_job(self):
        started = threading.Event()
        release = threading.Event()

        def fake_export(*_args):
            started.set()
            release.wait(2)

        with patch.object(markdown_export, "export_job", side_effect=fake_export):
            self.assertTrue(markdown_export.enqueue_export(self.data, self.jobs, self._job()))
            self.assertFalse(markdown_export.enqueue_export(self.data, self.jobs, self._job()))
            self.assertTrue(started.wait(2))
            release.set()
        for _ in range(50):
            if (str(self.jobs.resolve()), "job-1") not in markdown_export._pending:
                break
            time.sleep(0.01)
        self.assertNotIn((str(self.jobs.resolve()), "job-1"), markdown_export._pending)

    def test_paths_cannot_escape_job_directory(self):
        with self.assertRaises(markdown_export.MarkdownExportError):
            markdown_export.export_job(self.data, self.jobs, self._job(id="../outside"))
        with self.assertRaises(markdown_export.MarkdownExportError):
            markdown_export.export_job(self.data, self.jobs, self._job(source_file="../outside.pdf"))

    def test_library_index_excludes_incomplete_markdown_bundles(self):
        markdown_export.export_job(self.data, self.jobs, self._job())
        stale = self.jobs / "job-2" / "markdown"
        stale.mkdir(parents=True)
        (stale / "metadata.json").write_text(json.dumps({"job_id": "job-2", "title": "stale"}), encoding="utf-8")
        markdown_export._refresh_library_index(self.data, self.jobs)
        index = json.loads((self.data / "index.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["id"] for entry in index["entries"]], ["job-1"])

    def test_failed_publish_and_failed_rollback_keep_old_backup(self):
        target = self.job_dir / "markdown"
        target.mkdir()
        (target / "source.md").write_text("old export", encoding="utf-8")
        temporary = self.job_dir / ".new-markdown"
        temporary.mkdir()
        (temporary / "source.md").write_text("new export", encoding="utf-8")
        real_replace = markdown_export.os.replace
        calls = []

        def fail_publish_and_rollback(source, destination):
            calls.append((Path(source), Path(destination)))
            if len(calls) == 1:
                return real_replace(source, destination)
            raise OSError("simulated replace failure")

        with patch.object(markdown_export.os, "replace", side_effect=fail_publish_and_rollback):
            with self.assertRaisesRegex(markdown_export.MarkdownExportError, "备份"):
                markdown_export._atomic_publish(temporary, target)
        backups = list(self.job_dir.glob(".markdown.*.old"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "source.md").read_text(encoding="utf-8"), "old export")


if __name__ == "__main__":
    unittest.main()
