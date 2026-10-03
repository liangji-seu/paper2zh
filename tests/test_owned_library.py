from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from app import core


class OwnedLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data = root / "library"
        self.jobs = self.data / "jobs"
        self.jobs.mkdir(parents=True)
        self.patches = [patch.object(core, "DATA", self.data), patch.object(core, "JOBS", self.jobs)]
        for item in self.patches:
            item.start()
        core._catalog_synced.clear()
        core._jobs.clear()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def pdf(self) -> bytes:
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        stream = io.BytesIO()
        writer.write(stream)
        return stream.getvalue()

    def test_same_bytes_are_one_catalog_job_and_concurrent_imports_are_duplicates(self):
        content = self.pdf()
        results = []

        def import_one(index):
            results.append(core.create_job(f"{index}.pdf", content, "", "full", False, start_translation=False))

        threads = [threading.Thread(target=import_one, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len({job["id"] for job in results}), 1)
        self.assertEqual(sum(bool(job.get("duplicate")) for job in results), 3)
        self.assertEqual(len(list(self.jobs.glob("*/job.json"))), 1)

    def test_migration_copies_jobs_without_touching_old_data(self):
        old = Path(self.tmp.name) / "old"
        old_job = old / "jobs" / "legacy"
        old_job.mkdir(parents=True)
        (old_job / "job.json").write_text(json.dumps({"id": "legacy", "filename": "x.pdf", "source_sha256": "abc"}), encoding="utf-8")
        with patch.dict(os.environ, {"PAPER_TRANSLATOR_LEGACY_DATA_DIR": str(old)}):
            core.ensure_dirs()
        self.assertTrue((self.jobs / "legacy" / "job.json").is_file())
        self.assertTrue((old_job / "job.json").is_file())
        self.assertTrue((self.data / ".migration-v1.complete").is_file())

    def test_stale_catalog_reservation_is_recovered(self):
        content = self.pdf()
        source_hash = __import__("hashlib").sha256(content).hexdigest()
        core.ensure_dirs()
        connection = sqlite3.connect(self.data / "catalog.db")
        connection.execute("INSERT INTO documents(job_id, source_sha256, filename, display_title, created_at, status) VALUES (?, ?, ?, ?, ?, ?)", ("crashed", source_hash, "old.pdf", "old.pdf", "2000-01-01T00:00:00+00:00", ""))
        connection.execute("INSERT INTO source_index(source_sha256, job_id) VALUES (?, ?)", (source_hash, "crashed"))
        connection.commit()
        connection.close()
        job = core.create_job("recovered.pdf", content, "", "full", False, start_translation=False)
        self.assertFalse(job.get("duplicate"))
        self.assertTrue((self.jobs / job["id"] / "job.json").is_file())

    def test_workspace_preferences_are_whitelisted_and_clamped(self):
        self.assertEqual(core.save_workspace_preferences({"sidebar_width": 999, "sidebar_collapsed": True})["sidebar_width"], 420)
        self.assertEqual(core.get_workspace_preferences()["sidebar_collapsed"], True)

    def test_markdown_manifest_is_invalidated_after_retranslation(self):
        from app import markdown_export
        job = core.create_job("paper.pdf", self.pdf(), "", "full", False, start_translation=False)
        job_dir = self.jobs / job["id"]
        (job_dir / "markdown").mkdir()
        (job_dir / "markdown" / "source.md").write_text("source", encoding="utf-8")
        (job_dir / "markdown" / "translated.md").write_text("translated", encoding="utf-8")
        (job_dir / "translation-meta.json").write_text(json.dumps({"translated_sha256": "old"}), encoding="utf-8")
        (job_dir / "markdown" / "metadata.json").write_text(json.dumps({"version": markdown_export.EXPORT_VERSION, "schema_version": markdown_export.SCHEMA_VERSION, "converter": markdown_export.CONVERTER_NAME, "converter_version": markdown_export.CONVERTER_VERSION, "source_sha256": job["source_sha256"], "translated_sha256": "old", "scope": "full", "selected_pages": [1],}), encoding="utf-8")
        job.update({"status": "completed", "translation_scope": "full", "page_count": 1, "translated_file": "translate/paper.zh.pdf"})
        self.assertTrue(core._markdown_export_current(job))
        (job_dir / "translation-meta.json").write_text(json.dumps({"translated_sha256": "new"}), encoding="utf-8")
        self.assertFalse(core._markdown_export_current(job))

    def test_markdown_guard_checks_converter_schema_and_open_ended_pages(self):
        from app import markdown_export
        job = core.create_job("paper.pdf", self.pdf(), "", "trial", False, start_translation=False)
        job_dir = self.jobs / job["id"]
        (job_dir / "markdown").mkdir()
        (job_dir / "markdown" / "source.md").write_text("source", encoding="utf-8")
        (job_dir / "markdown" / "translated.md").write_text("translated", encoding="utf-8")
        (job_dir / "translation-meta.json").write_text(json.dumps({"translated_sha256": "same"}), encoding="utf-8")
        metadata = {"version": markdown_export.EXPORT_VERSION, "schema_version": markdown_export.SCHEMA_VERSION, "converter": markdown_export.CONVERTER_NAME, "converter_version": markdown_export.CONVERTER_VERSION, "source_sha256": job["source_sha256"], "translated_sha256": "same", "scope": "trial", "selected_pages": [3]}
        (job_dir / "markdown" / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        job.update({"status": "completed", "translation_scope": "trial", "pages": "3-", "page_count": 3, "translated_file": "translate/paper.zh.pdf"})
        self.assertTrue(core._markdown_export_current(job))
        metadata["schema_version"] = "old"
        (job_dir / "markdown" / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        self.assertFalse(core._markdown_export_current(job))

    def test_title_fallback_is_filename_and_pdf_stays_in_owned_job(self):
        job = core.create_job("paper.pdf", self.pdf(), "", "full", False, start_translation=False)
        job_dir = self.jobs / job["id"]
        translated = job_dir / "translate" / "paper.zh.pdf"
        translated.parent.mkdir()
        translated.write_bytes((job_dir / "source.pdf").read_bytes())
        bilingual = job_dir / "translate" / "paper.zh.bilingual.pdf"
        bilingual.write_bytes(translated.read_bytes())
        core._write_translation_meta(job, "full", translated, bilingual)
        job.update({"translated_file": "translate/paper.zh.pdf", "bilingual_file": "translate/paper.zh.bilingual.pdf", "status": "completed"})
        core.save_job(job)
        with patch.object(core, "_extract_translated_title", return_value="paper.pdf"):
            loaded = core.load_job(job["id"])
        self.assertEqual(loaded["display_title"], "paper.pdf")
        self.assertTrue(core.file_path(job["id"], "source").is_file())

    def test_title_uses_large_chinese_spans_and_ignores_date(self):
        class FakePage:
            rect = type("Rect", (), {"height": 842})()
            def get_text(self, _kind):
                return {"blocks": [{"lines": [{"spans": [
                    {"text": "2026-10-03", "size": 8, "bbox": (40, 20, 100, 30)},
                    {"text": "面向下肢外骨骼的", "size": 20, "bbox": (40, 55, 200, 80)},
                    {"text": "人机协同控制方法", "size": 20, "bbox": (40, 82, 200, 107)},
                    {"text": "作者 某某大学", "size": 9, "bbox": (40, 130, 160, 145)},
                ]}]}]}
        class FakeDocument:
            def __getitem__(self, _index): return FakePage()
            def close(self): pass
        fake_module = type("FakeFitz", (), {"open": staticmethod(lambda _path: FakeDocument())})
        with patch.dict(sys.modules, {"pymupdf": fake_module}):
            self.assertEqual(core._extract_translated_title(Path("synthetic.pdf"), "fallback.pdf"), "面向下肢外骨骼的人机协同控制方法")


if __name__ == "__main__":
    unittest.main()
