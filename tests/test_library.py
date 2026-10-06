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

    def _create_job(self, job_id: str) -> None:
        job_dir = self.jobs / job_id
        job_dir.mkdir()
        (job_dir / "job.json").write_text(json.dumps({"id": job_id, "filename": f"{job_id}.pdf", "status": "imported", "page_count": 1}), encoding="utf-8")

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

    def test_delete_nested_folder_reparents_direct_children_and_documents(self):
        self._create_job("document-2")
        self._create_job("document-3")
        root_id = library.apply_action({"action": "create_folder", "name": "根", "parent_id": None})["folders"][0]["id"]
        deleted_id = library.apply_action({"action": "create_folder", "name": "待删", "parent_id": root_id})["folders"][1]["id"]
        child_id = library.apply_action({"action": "create_folder", "name": "子级", "parent_id": deleted_id})["folders"][2]["id"]
        grandchild_id = library.apply_action({"action": "create_folder", "name": "孙级", "parent_id": child_id})["folders"][3]["id"]
        library.apply_action({"action": "move_document", "job_id": self.job_id, "folder_id": deleted_id})
        library.apply_action({"action": "move_document", "job_id": "document-2", "folder_id": child_id})
        library.apply_action({"action": "move_document", "job_id": "document-3", "folder_id": grandchild_id})

        deleted = library.apply_action({"action": "delete_folder", "id": deleted_id})

        self.assertEqual(
            deleted["folders"],
            [
                {"id": root_id, "name": "根", "parent_id": None},
                {"id": child_id, "name": "子级", "parent_id": root_id},
                {"id": grandchild_id, "name": "孙级", "parent_id": child_id},
            ],
        )
        self.assertEqual(
            deleted["documents"],
            {self.job_id: root_id, "document-2": child_id, "document-3": grandchild_id},
        )
        self.assertEqual(library.get_library(), deleted)
        for job_id in (self.job_id, "document-2", "document-3"):
            self.assertTrue((self.jobs / job_id / "job.json").is_file())

    def test_delete_top_level_folder_reparents_children_and_documents_to_root(self):
        self._create_job("document-2")
        deleted_id = library.apply_action({"action": "create_folder", "name": "顶层待删", "parent_id": None})["folders"][0]["id"]
        child_id = library.apply_action({"action": "create_folder", "name": "子级", "parent_id": deleted_id})["folders"][1]["id"]
        library.apply_action({"action": "move_document", "job_id": self.job_id, "folder_id": deleted_id})

        deleted = library.apply_action({"action": "delete_folder", "id": deleted_id})

        self.assertEqual(deleted["folders"], [{"id": child_id, "name": "子级", "parent_id": None}])
        self.assertIsNone(deleted["documents"][self.job_id])

    def test_delete_folder_rejects_unknown_or_invalid_parent_without_writing(self):
        with self.assertRaisesRegex(ValueError, "文件夹不存在"):
            library.apply_action({"action": "delete_folder", "id": "missing"})
        self.assertFalse((self.data / "library.json").exists())

        folder_id = "folder-1"
        original = {"folders": [{"id": folder_id, "name": "坏父级", "parent_id": "missing"}], "documents": {}}
        (self.data / "library.json").write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "父文件夹不存在"):
            library.apply_action({"action": "delete_folder", "id": folder_id})
        self.assertEqual(json.loads((self.data / "library.json").read_text(encoding="utf-8")), original)

    def test_delete_folder_write_failure_keeps_library_unchanged(self):
        folder_id = library.apply_action({"action": "create_folder", "name": "保留", "parent_id": None})["folders"][0]["id"]
        before = library.get_library()
        with patch.object(library, "_write", side_effect=RuntimeError("library locked")):
            with self.assertRaisesRegex(RuntimeError, "library locked"):
                library.apply_action({"action": "delete_folder", "id": folder_id})
        self.assertEqual(library.get_library(), before)


if __name__ == "__main__":
    unittest.main()
