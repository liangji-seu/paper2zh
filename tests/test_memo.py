from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import memo
from app.markdown_export import _preserve_user_notes


class MemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.jobs = Path(self.temp.name) / "jobs"
        self.job_dir = self.jobs / "job-1"
        self.job_dir.mkdir(parents=True)
        self.patch_jobs = patch.object(memo, "JOBS", self.jobs)
        self.patch_load = patch.object(memo, "load_job", side_effect=lambda job_id: {"id": job_id} if job_id == "job-1" else None)
        self.patch_jobs.start()
        self.patch_load.start()

    def tearDown(self):
        self.patch_load.stop()
        self.patch_jobs.stop()
        self.temp.cleanup()

    def test_missing_memo_is_empty(self):
        self.assertEqual(memo.read_memo("job-1"), {"content": "", "updated_at": None})

    def test_save_and_reload_round_trip(self):
        saved = memo.save_memo("job-1", "# 要点\n\n- 方法\n- 局限\n")
        self.assertEqual(saved["content"], "# 要点\n\n- 方法\n- 局限\n")
        self.assertTrue(saved["updated_at"])
        path = self.job_dir / "markdown" / "notes.md"
        self.assertEqual(path.read_text(encoding="utf-8"), "# 要点\n\n- 方法\n- 局限\n")
        loaded = memo.read_memo("job-1")
        self.assertEqual(loaded["content"], saved["content"])
        self.assertEqual(loaded["updated_at"], saved["updated_at"])

    def test_too_long_and_invalid_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            memo.save_memo("job-1", "x" * (memo.MAX_MEMO_CHARS + 1))
        with self.assertRaises(ValueError):
            memo.read_memo("../etc")
        with self.assertRaises(FileNotFoundError):
            memo.read_memo("missing")

    def test_generated_markdown_export_preserves_memo(self):
        target = self.job_dir / "markdown"
        target.mkdir(parents=True)
        (target / "notes.md").write_text("# my memo\n", encoding="utf-8")
        staging = self.job_dir / "staging"
        staging.mkdir()
        (staging / "source.md").write_text("# source", encoding="utf-8")
        _preserve_user_notes(target, staging)
        self.assertEqual((staging / "notes.md").read_text(encoding="utf-8"), "# my memo\n")


if __name__ == "__main__":
    unittest.main()
