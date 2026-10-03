from __future__ import annotations

import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.pdf_worker import pdf_serialized


RUNTIME = Path(__file__).resolve().parents[1] / ".runtime311"
if RUNTIME.is_dir() and str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))


class PdfWorkerTests(unittest.TestCase):
    def test_concurrent_real_fitz_operations_use_one_worker_thread(self):
        try:
            import fitz
        except ImportError as exc:  # pragma: no cover - requirements include PyMuPDF
            self.skipTest(f"PyMuPDF unavailable: {exc}")

        @pdf_serialized
        def make_text(value: str):
            document = fitz.open()
            try:
                page = document.new_page()
                page.insert_text((40, 60), value, fontsize=11)
                return threading.get_ident(), page.get_text().strip()
            finally:
                document.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(make_text, [f"value-{index}" for index in range(24)]))
        self.assertEqual(len({thread_id for thread_id, _text in results}), 1)
        self.assertEqual({text for _thread_id, text in results}, {f"value-{index}" for index in range(24)})

    def test_worker_exception_returns_to_calling_thread(self):
        caller = threading.get_ident()

        @pdf_serialized
        def fail():
            raise ValueError(f"worker={threading.get_ident()}")

        with self.assertRaisesRegex(ValueError, r"worker=\d+") as raised:
            fail()
        self.assertNotIn(str(caller), str(raised.exception))

    def test_decorated_reentry_does_not_deadlock(self):
        @pdf_serialized
        def inner():
            return threading.get_ident()

        @pdf_serialized
        def outer():
            return threading.get_ident(), inner()

        outer_thread, inner_thread = outer()
        self.assertEqual(outer_thread, inner_thread)


if __name__ == "__main__":
    unittest.main()
