from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from app import core
from app.progress import parse_engine_progress


class V1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.jobs = self.data / "jobs"
        self.data.mkdir()
        self.jobs.mkdir()
        self.patches = [patch.object(core, "DATA", self.data), patch.object(core, "JOBS", self.jobs), patch.object(core, "SETTINGS_PATH", self.data / "settings.json")]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def pdf_bytes(self):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        output = self.data / "input.pdf"
        with output.open("wb") as stream:
            writer.write(stream)
        return output.read_bytes()

    def test_structured_progress_is_real_and_unknown_stays_indeterminate(self):
        event = parse_engine_progress("DEBUG event {'type': 'progress_update', 'stage': 'Translate Paragraphs', 'stage_current': 3, 'stage_total': 10, 'overall_progress': 42.5}")
        self.assertEqual(event["stage"], "Translate Paragraphs")
        self.assertEqual(event["progress"], 42.5)
        self.assertFalse(event["indeterminate"])
        ended_stage = parse_engine_progress("DEBUG event {'type': 'progress_end', 'stage': 'Translate Paragraphs'}")
        self.assertIsNone(ended_stage["progress"])
        self.assertTrue(ended_stage["indeterminate"])
        self.assertIsNone(parse_engine_progress("engine is working"))

    def test_full_translation_reuses_matching_manifest_without_api(self):
        job = core.create_job("paper.pdf", self.pdf_bytes(), "", "full", False, start_translation=False)
        job_dir = self.jobs / job["id"]
        translated = job_dir / "translate" / "paper.zh.pdf"
        bilingual = job_dir / "translate" / "paper.zh.bilingual.pdf"
        translated.parent.mkdir()
        translated.write_bytes((job_dir / "source.pdf").read_bytes())
        bilingual.write_bytes((job_dir / "source.pdf").read_bytes())
        core._write_translation_meta(job, "full", translated, bilingual)
        with patch.object(core, "read_settings", side_effect=AssertionError("reuse must not read provider settings")):
            core._worker(job)
        current = core.load_job(job["id"])
        self.assertEqual(current["status"], "completed")
        self.assertTrue(current["translation_reused"])
        self.assertEqual(current["translation_scope"], "full")

    def test_source_change_invalidates_old_manifest_and_output_is_not_overwritten(self):
        job = core.create_job("paper.pdf", self.pdf_bytes(), "", "full", False, start_translation=False)
        job_dir = self.jobs / job["id"]
        external = job_dir / "translate" / "paper.zh.pdf"
        external.parent.mkdir()
        external.write_bytes(b"external-output")
        generated = job_dir / "generated.pdf"
        generated.write_bytes((job_dir / "source.pdf").read_bytes())
        published, _ = core._publish_translation_outputs(job, generated, generated)
        self.assertNotEqual(published, external)
        self.assertEqual(external.read_bytes(), b"external-output")
        published.write_bytes(b"manual-edit")
        published_again, _ = core._publish_translation_outputs(job, generated, generated)
        self.assertNotEqual(published_again, published)
        self.assertEqual(published.read_bytes(), b"manual-edit")
        with (job_dir / "source.pdf").open("ab") as stream:
            stream.write(b"changed")
        self.assertFalse(core._translation_is_current(core.load_job(job["id"])))
        self.assertIsNone(core.file_path(job["id"], "translated"))

    def test_local_import_copies_pdf_without_exposing_original_path(self):
        source = self.data / "picked.pdf"
        source.write_bytes(self.pdf_bytes())
        job = core.create_job_from_path(str(source), "", "full", False)
        self.assertEqual(job["source_origin"], "desktop-local")
        self.assertNotIn(str(source), json.dumps(job, ensure_ascii=False))
        self.assertEqual((self.jobs / job["id"] / "source.pdf").read_bytes(), source.read_bytes())

    def test_native_full_translation_writes_and_reopens_external_sidecar(self):
        papers = self.data.parent / "papers"
        papers.mkdir()
        source = papers / "paper.pdf"
        source.write_bytes(self.pdf_bytes())
        job = core.create_job_from_path(str(source), "", "full", False)
        job_dir = self.jobs / job["id"]
        generated = job_dir / "generated.pdf"
        generated.write_bytes((job_dir / "source.pdf").read_bytes())
        translated, bilingual = core._publish_translation_outputs(job, generated, generated)
        self.assertIsNone(core._publish_external_full_outputs(job, translated, bilingual))
        external = papers / "translate" / "paper.zh.pdf"
        manifest = papers / "translate" / "paper.zh.manifest.json"
        self.assertTrue(external.is_file())
        self.assertTrue(manifest.is_file())
        shutil.rmtree(job_dir)
        reopened = core.create_job_from_path(str(source), "", "full", False)
        self.assertEqual(reopened["status"], "completed")
        self.assertTrue(reopened["translation_reused"])
        self.assertNotIn(str(source), json.dumps(reopened, ensure_ascii=False))
        self.assertTrue(core.file_path(reopened["id"], "translated").is_file())

    def test_demo_full_has_demo_scope_and_cannot_be_full_cache(self):
        job = core.create_job("demo.pdf", self.pdf_bytes(), "", "full", True)
        for _ in range(50):
            current = core.load_job(job["id"])
            if current["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["translation_scope"], "demo")
        self.assertTrue((self.jobs / job["id"] / "demo").is_dir())
        self.assertFalse((self.jobs / job["id"] / "translate").exists())
        self.assertEqual(core._read_translation_meta(current).get("scope"), "demo")


if __name__ == "__main__":
    unittest.main()
