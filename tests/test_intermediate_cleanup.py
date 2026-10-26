from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import core


class IntermediateCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.jobs = Path(self.temp.name)
        self.patch_jobs = patch.object(core, "JOBS", self.jobs)
        self.patch_jobs.start()

    def tearDown(self):
        self.patch_jobs.stop()
        self.temp.cleanup()

    def _job(self, job_id: str, status: str, translated: bool = True) -> Path:
        job_dir = self.jobs / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "job.json").write_text(json.dumps({"id": job_id, "status": status, "translated_file": "translate/out.zh.pdf" if translated else ""}), encoding="utf-8")
        (job_dir / "source.pdf").write_bytes(b"%PDF-1.4")
        (job_dir / "translate").mkdir()
        (job_dir / "translate" / "out.zh.pdf").write_bytes(b"%PDF-1.4")
        (job_dir / "markdown").mkdir()
        (job_dir / "markdown" / "notes.md").write_text("# memo", encoding="utf-8")
        (job_dir / "babeldoc-work").mkdir()
        (job_dir / "babeldoc-work" / "temp.pdf").write_bytes(b"x")
        (job_dir / "babeldoc-output").mkdir()
        (job_dir / "babeldoc-output" / "temp.pdf").write_bytes(b"x")
        return job_dir

    def test_startup_sweep_only_cleans_completed_jobs_with_final_pdf(self):
        completed = self._job("completed", "completed")
        running = self._job("running", "running")
        missing = self._job("missing", "completed", translated=False)
        self.assertEqual(core.cleanup_completed_intermediates(), 1)
        self.assertFalse((completed / "babeldoc-work").exists())
        self.assertFalse((completed / "babeldoc-output").exists())
        self.assertTrue((completed / "translate" / "out.zh.pdf").is_file())
        self.assertTrue((completed / "markdown" / "notes.md").is_file())
        self.assertTrue((running / "babeldoc-work").is_dir())
        self.assertTrue((missing / "babeldoc-work").is_dir())

    def test_job_cleanup_removes_only_intermediate_dirs(self):
        job_dir = self._job("job", "completed")
        core._cleanup_job_intermediates("job")
        self.assertFalse((job_dir / "babeldoc-work").exists())
        self.assertFalse((job_dir / "babeldoc-output").exists())
        self.assertTrue((job_dir / "source.pdf").is_file())
        self.assertTrue((job_dir / "translate" / "out.zh.pdf").is_file())


if __name__ == "__main__":
    unittest.main()
