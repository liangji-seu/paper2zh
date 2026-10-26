from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import core


class FolderPdfTests(unittest.TestCase):
    def test_recursive_sorted_filter_and_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sub").mkdir()
            (root / "b.pdf").write_bytes(b"%PDF-1.4")
            (root / "sub" / "A.PDF").write_bytes(b"%PDF-1.4")
            (root / "sub" / "notes.txt").write_text("x", encoding="utf-8")
            (root / "sub" / "c.pd").write_bytes(b"x")
            paths = core.list_folder_pdfs(root)
            self.assertEqual([path.relative_to(root).as_posix() for path in paths], ["b.pdf", "sub/A.PDF"])
            limited = core.list_folder_pdfs(root, 1)
            self.assertEqual(len(limited), 1)

    def test_missing_folder_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                core.list_folder_pdfs(Path(directory) / "missing")


class RecoveryTests(unittest.TestCase):
    def tearDown(self):
        core._recovered_interrupts = False

    def test_running_and_queued_jobs_become_retryable_failures(self):
        core._recovered_interrupts = False
        rows = [
            {"job_id": "job-1", "status": "running"},
            {"job_id": "job-2", "status": "queued"},
            {"job_id": "job-3", "status": "completed"},
        ]
        jobs = {
            "job-1": {"id": "job-1", "status": "running"},
            "job-2": {"id": "job-2", "status": "queued"},
        }
        saved = []

        def fake_load(job_id):
            return dict(jobs[job_id]) if job_id in jobs else None

        def fake_save(job):
            saved.append(dict(job))

        with patch.object(core.catalog, "list_documents", return_value=rows), \
                patch.object(core, "load_job", side_effect=fake_load), \
                patch.object(core, "save_job", side_effect=fake_save):
            recovered = core.recover_interrupted_jobs()
            self.assertEqual(sorted(recovered), ["job-1", "job-2"])
            self.assertTrue(all(job["status"] == "failed" for job in saved))
            self.assertTrue(all(job["error"] == "上次运行被中断，请重新翻译。" for job in saved))
            self.assertEqual(core.recover_interrupted_jobs(), [])

    def test_recovery_is_idempotent_after_first_run(self):
        core._recovered_interrupts = True
        with patch.object(core.catalog, "list_documents") as lister:
            self.assertEqual(core.recover_interrupted_jobs(), [])
            lister.assert_not_called()


if __name__ == "__main__":
    unittest.main()
