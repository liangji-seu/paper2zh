from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

from pypdf import PdfWriter

from app import core
from app import server


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.jobs = self.data / "jobs"
        self.settings = self.data / "settings.json"
        self.data.mkdir()
        self.jobs.mkdir()
        self.patches = [patch.object(core, "DATA", self.data), patch.object(core, "JOBS", self.jobs), patch.object(core, "SETTINGS_PATH", self.settings)]
        for p in self.patches: p.start()
        core._jobs.clear()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def pdf(self, pages=2):
        writer = PdfWriter()
        for _ in range(pages): writer.add_blank_page(width=612, height=792)
        out = self.data / "input.pdf"
        with out.open("wb") as f: writer.write(f)
        return out.read_bytes()

    def test_settings_mask_and_endpoint_reuse_guard(self):
        with patch.object(core, "protect", return_value="dpapi:test"), patch.object(core, "unprotect", return_value="sk-test-secret"):
            core.save_settings({"api_key": "sk-test-secret"})
            raw = self.settings.read_text(encoding="utf-8")
            self.assertNotIn("sk-test-secret", raw)
            self.assertTrue(core.public_settings()["has_api_key"])
            with self.assertRaises(ValueError): core.save_settings({"base_url": "https://other.example/v1"})
            saved = core.save_settings({"base_url": "https://other.example/v1", "reuse_existing_key": True})
            self.assertTrue(saved["has_api_key"])

    def test_page_validation(self):
        self.assertEqual(core.parse_pages("1,2", 2), "1,2")
        with self.assertRaises(ValueError): core.parse_pages("0", 2)
        with self.assertRaises(ValueError): core.parse_pages("3", 2)

    def test_demo_job_completes_and_outputs_pdf(self):
        job = core.create_job("a.pdf", self.pdf(), "1", "trial", True)
        for _ in range(50):
            current = core.load_job(job["id"])
            if current["status"] in {"completed", "failed"}: break
            time.sleep(0.02)
        self.assertEqual(current["status"], "completed")
        self.assertTrue(core.file_path(job["id"], "translated").is_file())
        self.assertTrue(core.file_path(job["id"], "bilingual").is_file())
        self.assertTrue(core.render_page(job["id"], "source", 1, 100).startswith(b"\x89PNG"))
        self.assertEqual(current["page_count"], 2)

    def test_connection_sends_minimal_request_and_classifies_success(self):
        with patch.object(core, "protect", return_value="dpapi:test"), patch.object(core, "unprotect", return_value="sk-test-secret"):
            core.save_settings({"api_key": "sk-test-secret"})
            response = MagicMock()
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            response.read.return_value = b"{}"
            with patch.object(server.urllib.request, "urlopen", return_value=response) as call:
                result = server.test_connection()
            self.assertTrue(result["ok"])
            request = call.call_args.args[0]
            self.assertIn("Bearer sk-test-secret", request.headers.get("Authorization", ""))
            self.assertIn(b"max_tokens", request.data)

    def test_import_only_keeps_source_without_starting_translation(self):
        job = core.create_job("import.pdf", self.pdf(), "", "import", False, start_translation=False)
        self.assertEqual(job["status"], "imported")
        self.assertTrue(core.file_path(job["id"], "source").is_file())
        self.assertIsNone(core.file_path(job["id"], "translated"))


if __name__ == "__main__":
    unittest.main()
