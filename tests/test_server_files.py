from __future__ import annotations

import http.client
import json
import socket
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from app import server


class FileResponseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.payload = bytes(range(256)) * 4096
        self.file = self.root / "sample.pdf"
        self.file.write_bytes(self.payload)
        self.big_file = self.root / "big.pdf"
        self.big_file.write_bytes(b"x" * (16 * 1024 * 1024))
        self.errors: list[type[BaseException]] = []
        metrics = {}
        test_root = self.root
        parent = self

        class TestHandler(server.Handler):
            def handle_error(self, _request, _client_address):
                parent.errors.append(sys.exc_info()[0])

            def do_GET(self):
                if self.path.startswith("/measure-old"):
                    self._measure("old", self._send_old)
                    return
                if self.path.startswith("/measure-new"):
                    self._measure("new", super()._send_file)
                    return
                super().do_GET()

            def _send_old(self, path, inline):
                content_type = server.mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                disposition = "inline" if inline else "attachment"
                self._send(200, path.read_bytes(), content_type, {"Content-Disposition": f'{disposition}; filename="{path.name}"'})

            def _measure(self, name, sender):
                tracemalloc.start()
                tracemalloc.reset_peak()
                try:
                    sender(test_root / "big.pdf", True)
                finally:
                    _current, peak = tracemalloc.get_traced_memory()
                    tracemalloc.stop()
                    metrics[name] = peak

        self.handler = TestHandler
        self.metrics = metrics
        self.previous_static = server.STATIC
        self.previous_port = server.PORT
        self.previous_origins = server.LOCAL_ORIGINS
        self.httpd = ThreadingHTTPServer((server.HOST, 0), TestHandler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        server.STATIC = self.root
        server.PORT = self.port
        server.LOCAL_ORIGINS = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.STATIC = self.previous_static
        server.PORT = self.previous_port
        server.LOCAL_ORIGINS = self.previous_origins
        self.tmp.cleanup()

    def request(self, path="/static/sample.pdf", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            body = response.read()
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    def drain_raw(self, path):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            request = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nConnection: close\r\n\r\n".encode()
            sock.sendall(request)
            sink = bytearray(64 * 1024)
            while sock.recv_into(sink):
                pass
        finally:
            sock.close()

    def test_full_and_range_responses_do_not_call_read_bytes(self):
        with patch.object(Path, "read_bytes", side_effect=AssertionError("full PDF must stream")):
            status, headers, body = self.request()
            self.assertEqual(status, 200)
            self.assertEqual(body, self.payload)
            self.assertEqual(headers["Content-Length"], str(len(self.payload)))
            self.assertEqual(headers["Accept-Ranges"], "bytes")

            status, headers, body = self.request(headers={"Range": "bytes=4-9"})
            self.assertEqual(status, 206)
            self.assertEqual(body, self.payload[4:10])
            self.assertEqual(headers["Content-Range"], f"bytes 4-9/{len(self.payload)}")

            status, headers, body = self.request(headers={"Range": "bytes=-7"})
            self.assertEqual(status, 206)
            self.assertEqual(body, self.payload[-7:])
            self.assertEqual(headers["Content-Range"], f"bytes {len(self.payload) - 7}-{len(self.payload) - 1}/{len(self.payload)}")

            status, headers, body = self.request(headers={"Range": "bytes=7-"})
            self.assertEqual(status, 206)
            self.assertEqual(body, self.payload[7:])
            self.assertEqual(headers["Content-Range"], f"bytes 7-{len(self.payload) - 1}/{len(self.payload)}")

    def test_invalid_or_unsupported_range_is_safe(self):
        status, headers, body = self.request(headers={"Range": f"bytes={len(self.payload)}-"})
        self.assertEqual(status, 416)
        self.assertEqual(body, b"")
        self.assertEqual(headers["Content-Range"], f"bytes */{len(self.payload)}")

        for value in ("bytes=0-1,4-5", "items=0-1"):
            status, _headers, body = self.request(headers={"Range": value})
            self.assertEqual(status, 200)
            self.assertEqual(body, self.payload)

        status, _headers, body = self.request(headers={"Range": "bytes=4-9", "If-Range": "stale"})
        self.assertEqual(status, 200)
        self.assertEqual(body, self.payload)

    def test_client_disconnect_is_quiet_and_health_survives(self):
        self.drain_raw("/static/big.pdf")
        time.sleep(0.1)
        status, _headers, body = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertIn(b'"ok": true', body)
        self.assertEqual(self.errors, [])

    def test_streaming_peak_is_lower_than_read_bytes_for_16mib_sink(self):
        self.drain_raw("/measure-old")
        self.drain_raw("/measure-new")
        old_peak = self.metrics["old"]
        new_peak = self.metrics["new"]
        print(f"service tracemalloc peak bytes: read_bytes={old_peak}, chunked={new_peak}")
        self.assertGreater(old_peak, 8 * 1024 * 1024)
        self.assertGreater(old_peak, new_peak * 4)

    def test_markdown_location_route_returns_only_verified_completed_bundle(self):
        jobs = self.root / "library" / "jobs"
        markdown = jobs / "job-1" / "markdown"
        markdown.mkdir(parents=True)
        (jobs / "job-1" / "job.json").write_text(json.dumps({"id": "job-1", "status": "completed"}), encoding="utf-8")
        (markdown / "source.md").write_text("# source", encoding="utf-8")
        (markdown / "translated.md").write_text("# translated", encoding="utf-8")
        (markdown / "metadata.json").write_text(json.dumps({
            "version": "1", "schema_version": "2", "job_id": "job-1",
            "converter": "PyMuPDF with pypdf fallback", "converter_version": "1",
        }), encoding="utf-8")
        previous_jobs = server.JOBS
        server.JOBS = jobs
        try:
            status, _headers, body = self.request("/api/jobs/job-1/markdown-location")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"directory": str(markdown.resolve())})

            status, _headers, body = self.request("/api/jobs/missing/markdown-location")
            self.assertEqual(status, 404)
            self.assertIn("任务目录不存在", body.decode("utf-8"))

            status, _headers, body = self.request("/api/jobs/%2e%2e/markdown-location")
            self.assertEqual(status, 400)
            self.assertIn("不安全", body.decode("utf-8"))
        finally:
            server.JOBS = previous_jobs


if __name__ == "__main__":
    unittest.main()
