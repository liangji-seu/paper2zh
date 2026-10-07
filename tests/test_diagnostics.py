from __future__ import annotations

import io
import http.client
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import diagnostics
from app import server


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.previous_log_dir = diagnostics._data_dir
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.temp.name)
        diagnostics.configure(self.root)

    def tearDown(self):
        self.temp.cleanup()
        diagnostics._data_dir = self.previous_log_dir

    def _records(self):
        return [json.loads(line) for line in (self.root / "logs" / "diagnostics.log").read_text(encoding="utf-8").splitlines()]

    def test_records_safe_fields_without_exception_message_or_sensitive_values(self):
        try:
            raise RuntimeError("APIKEY-SHOULD-NOT-APPEAR C:/Users/secret/paper.pdf")
        except RuntimeError as exc:
            diagnostics.record_exception("translation_failed", exc, job_id="job-1", fields={"phase": "engine", "encoding": "utf8"})
        item = self._records()[0]
        self.assertEqual(item["exception"], "RuntimeError")
        self.assertEqual(item["job_id"], "job-1")
        self.assertEqual(item["phase"], "engine")
        self.assertNotIn("SHOULD-NOT-APPEAR", json.dumps(item))
        self.assertNotIn("C:/Users", json.dumps(item))
        self.assertEqual(set(item["stack"][-1]), {"file", "function", "line"})

    def test_rotation_keeps_current_and_three_backups(self):
        with patch.object(diagnostics, "MAX_LOG_BYTES", 300):
            for index in range(40):
                diagnostics.record("rotation_event", fields={"phase": "worker", "count": index})
        logs = sorted((self.root / "logs").glob("diagnostics.log*"))
        self.assertLessEqual(len(logs), 4)
        self.assertTrue((self.root / "logs" / "diagnostics.log").exists())
        self.assertTrue(any(path.name.endswith(".3") for path in logs))

    def test_logging_failure_is_best_effort(self):
        broken = self.root / "a-file"
        broken.write_text("sentinel", encoding="utf-8")
        diagnostics.configure(broken)
        diagnostics.record("should_not_raise")

    def test_export_contains_only_diagnostics_and_version(self):
        diagnostics.record("export_event")
        (self.root / "logs" / "engine.log").write_text("secret", encoding="utf-8")
        with zipfile.ZipFile(io.BytesIO(diagnostics.export_bytes())) as archive:
            self.assertEqual(set(archive.namelist()), {"version.json", "diagnostics.log"})
            self.assertNotIn(b"secret", archive.read("version.json"))

    def test_client_error_has_enum_and_field_validation(self):
        self.assertEqual(diagnostics.client_error({"event": "pdf_preview_failed", "pdf": "translated", "page": 3, "job_id": "job-1", "error": "raw body"}), {"ok": True})
        self.assertEqual(self._records()[0]["event"], "pdf_preview_failed")
        with self.assertRaises(ValueError):
            diagnostics.client_error({"event": "arbitrary_user_event"})
        with self.assertRaises(ValueError):
            diagnostics.client_error({"event": "pdf_preview_failed", "pdf": "other"})

    def test_http_get_exception_is_logged_without_exception_text(self):
        class BoomHandler(server.Handler):
            def do_GET(self):
                if self.path == "/diagnostic-boom":
                    raise RuntimeError("secret request body and local paper path")
                return super().do_GET()

        httpd = server.ThreadingHTTPServer((server.HOST, 0), BoomHandler)
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(server.HOST, httpd.server_address[1], timeout=2)
        try:
            connection.request("GET", "/diagnostic-boom")
            with self.assertRaises((http.client.RemoteDisconnected, ConnectionResetError, http.client.BadStatusLine)):
                connection.getresponse()
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
        time.sleep(0.05)
        item = self._records()[0]
        self.assertEqual(item["event"], "http_unhandled")
        self.assertEqual(item["exception"], "RuntimeError")
        self.assertNotIn("secret request body", json.dumps(item))


if __name__ == "__main__":
    unittest.main()
