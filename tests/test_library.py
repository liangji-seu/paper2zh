from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import core, library


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.jobs = self.data / "jobs"
        self.data.mkdir()
        self.jobs.mkdir()
        self.job_id = "document-1"
        job_dir = self.jobs / self.job_id
        job_dir.mkdir()
        (job_dir / "job.json").write_text(json.dumps({"id": self.job_id, "filename": "paper.pdf", "status": "imported", "page_count": 1}), encoding="utf-8")
        self.patches = [patch.object(core, "DATA", self.data), patch.object(core, "JOBS", self.jobs)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_nested_create_persists_and_renames(self):
        root = library.apply_action({"action": "create_folder", "name": "论文库", "parent_id": None})
        root_id = root["folders"][0]["id"]
        nested = library.apply_action({"action": "create_folder", "name": "初稿", "parent_id": root_id})
        nested_id = nested["folders"][1]["id"]
        renamed = library.apply_action({"action": "rename_folder", "id": nested_id, "name": "已阅读"})
        self.assertEqual(renamed["folders"][1], {"id": nested_id, "name": "已阅读", "parent_id": root_id})
        reopened = library.get_library()
        self.assertEqual(reopened, renamed)
        self.assertTrue((self.data / "library.json").is_file())

    def test_move_document_and_return_to_root(self):
        folder = library.apply_action({"action": "create_folder", "name": "重点", "parent_id": None})["folders"][0]["id"]
        moved = library.apply_action({"action": "move_document", "job_id": self.job_id, "folder_id": folder})
        self.assertEqual(moved["documents"][self.job_id], folder)
        root = library.apply_action({"action": "move_document", "job_id": self.job_id, "folder_id": None})
        self.assertIsNone(root["documents"][self.job_id])

    def test_cycle_and_invalid_parent_are_rejected(self):
        first = library.apply_action({"action": "create_folder", "name": "一", "parent_id": None})["folders"][0]["id"]
        second = library.apply_action({"action": "create_folder", "name": "二", "parent_id": first})["folders"][1]["id"]
        with self.assertRaises(ValueError):
            library.apply_action({"action": "move_folder", "id": first, "parent_id": second})
        with self.assertRaises(ValueError):
            library.apply_action({"action": "create_folder", "name": "缺失父级", "parent_id": "missing"})
        with self.assertRaises(ValueError):
            library.apply_action({"action": "move_document", "job_id": "missing-job", "folder_id": None})


if __name__ == "__main__":
    unittest.main()
