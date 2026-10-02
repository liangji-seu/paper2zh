from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from app import core


SUMMARY = """
INFO:babeldoc.main:Total tokens: 5910 main.py:772
INFO:babeldoc.main:Prompt tokens: 4377 main.py:773
INFO:babeldoc.main:Completion tokens: main.py:774
                             1533
INFO:babeldoc.main:Cache hit prompt main.py:775
                             tokens: 40
"""


class TranslationQualityTests(unittest.TestCase):
    def test_usage_parses_wrapped_provider_summary_and_accumulates(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            job_dir = jobs / "test-job"
            job_dir.mkdir()
            (job_dir / "engine.log").write_text(SUMMARY, encoding="utf-8")
            with patch.object(core, "JOBS", jobs):
                job = {"id": "test-job", "mode": "full", "pages": "", "status": "completed"}
                core._record_engine_usage(job)
                self.assertEqual(job["token_usage"]["total_tokens"], 5910)
                self.assertEqual(job["token_usage"]["prompt_tokens"], 4377)
                self.assertEqual(job["token_usage"]["completion_tokens"], 1533)
                self.assertEqual(job["token_usage"]["cache_hit_prompt_tokens"], 40)
                core._record_engine_usage(job)
                self.assertEqual(job["token_usage"]["total_tokens"], 11820)
                self.assertEqual(job["token_usage"]["run_count"], 2)

    def test_incomplete_body_fails_instead_of_claiming_success(self):
        runtime = core.ROOT / (".runtime311" if sys.version_info[:2] == (3, 11) else ".runtime")
        if runtime.is_dir() and str(runtime) not in sys.path:
            sys.path.insert(0, str(runtime))
        try:
            import pymupdf as fitz
        except ImportError:
            try:
                import fitz
            except ImportError:
                self.skipTest("PyMuPDF unavailable")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            translated = Path(directory) / "translated.pdf"
            doc = fitz.open()
            page = doc.new_page()
            for index in range(25):
                page.insert_text((40, 40 + 20 * index), "Synthetic English methods paragraph for translation verification.", fontsize=11)
            doc.save(source)
            doc.close()
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((40, 40), "Short heading", fontsize=11)
            doc.save(translated)
            doc.close()
            with self.assertRaisesRegex(RuntimeError, "正文疑似缺失"):
                core._validate_translation_output(source, translated, {"mode": "full", "pages": ""})


if __name__ == "__main__":
    unittest.main()
