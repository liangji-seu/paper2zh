from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from app import annotations, core


def fitz_module():
    runtime = core.ROOT / ".runtime"
    if str(runtime) not in __import__("sys").path:
        __import__("sys").path.insert(0, str(runtime))
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    return fitz


class AnnotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.jobs = self.data / "jobs"
        self.data.mkdir()
        self.jobs.mkdir()
        self.job_id = "annotation-job"
        self.job_dir = self.jobs / self.job_id
        self.job_dir.mkdir()
        source = self.job_dir / "source.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with source.open("wb") as stream:
            writer.write(stream)
        self.source_bytes = source.read_bytes()
        (self.job_dir / "job.json").write_text(json.dumps({"id": self.job_id, "filename": "paper.pdf", "page_count": 1, "translated_file": None}), encoding="utf-8")
        self.patches = [patch.object(core, "DATA", self.data), patch.object(core, "JOBS", self.jobs)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_save_reopen_delete(self):
        _, revision = annotations.revision_for(self.job_id, "source")
        added = annotations.add_annotation(self.job_id, "source", {"revision": revision, "action": "add", "page": 1, "type": "highlight", "color": "#FFE066", "rects": [[100, 600, 200, 650]]})
        self.assertEqual(len(added["annotations"]), 1)
        reopened = annotations.get_annotations(self.job_id, "source")
        self.assertEqual(reopened["annotations"], added["annotations"])
        deleted = annotations.delete_annotation(self.job_id, "source", {"revision": revision, "action": "delete", "id": added["annotations"][0]["id"]})
        self.assertEqual(deleted["annotations"], [])
        self.assertEqual(annotations.get_annotations(self.job_id, "source")["annotations"], [])

    def test_revision_change_keeps_old_group_without_applying_it(self):
        _, old_revision = annotations.revision_for(self.job_id, "source")
        annotations.add_annotation(self.job_id, "source", {"revision": old_revision, "action": "add", "page": 1, "type": "underline", "color": "#74C0FC", "rects": [[100, 600, 200, 620]]})
        source = self.job_dir / "source.pdf"
        source.write_bytes(self.source_bytes + b"\n")
        new_revision = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertNotEqual(old_revision, new_revision)
        current = annotations.get_annotations(self.job_id, "source")
        self.assertEqual(current["revision"], new_revision)
        self.assertEqual(current["annotations"], [])
        stored = json.loads((self.job_dir / "annotations.json").read_text(encoding="utf-8"))
        self.assertIn(f"source:{old_revision}", stored)

    def test_revision_mismatch_is_rejected(self):
        with self.assertRaises(annotations.RevisionMismatch):
            annotations.add_annotation(self.job_id, "source", {"revision": "0" * 64, "action": "add", "page": 1, "type": "highlight", "color": "#FFE066", "rects": [[100, 600, 200, 650]]})

    def test_export_contains_standard_annotations_and_keeps_original(self):
        source = self.job_dir / "source.pdf"
        rotated_writer = PdfWriter()
        rotated_page = rotated_writer.add_blank_page(width=612, height=792)
        rotated_page.rotate(90)
        with source.open("wb") as stream:
            rotated_writer.write(stream)
        original_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        _, revision = annotations.revision_for(self.job_id, "source")
        annotations.add_annotation(self.job_id, "source", {"revision": revision, "action": "add", "page": 1, "type": "highlight", "color": "#FFE066", "rects": [[100, 600, 200, 650], [250, 500, 300, 530]]})
        annotations.add_annotation(self.job_id, "source", {"revision": revision, "action": "add", "page": 1, "type": "underline", "color": "#FAA2C1", "rects": [[100, 450, 200, 470]]})
        exported, exported_revision = annotations.export_annotated(self.job_id, "source")
        self.assertEqual(exported_revision, revision)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_digest)
        document = fitz_module().open(stream=exported, filetype="pdf")
        try:
            annot_types = [annot.type[1] for annot in document[0].annots()]
            self.assertIn("Highlight", annot_types)
            self.assertIn("Underline", annot_types)
        finally:
            document.close()

    def test_rotated_text_bbox_aligns_with_exported_vertices(self):
        fitz = fitz_module()
        source = self.job_dir / "source.pdf"
        document = fitz.open()
        page = document.new_page(width=612, height=792)
        page.insert_text((100, 120), "TARGET", fontsize=20)
        page.set_rotation(90)
        words = page.get_text("words")
        self.assertTrue(words)
        text_rect = fitz.Rect(*words[0][:4])
        pdf_matrix = ~page.transformation_matrix
        pdf_rect = text_rect * pdf_matrix
        document.save(str(source))
        document.close()

        _, revision = annotations.revision_for(self.job_id, "source")
        annotations.add_annotation(self.job_id, "source", {"revision": revision, "action": "add", "page": 1, "type": "highlight", "color": "#FFE066", "rects": [[pdf_rect.x0, pdf_rect.y0, pdf_rect.x1, pdf_rect.y1]]})
        exported, _ = annotations.export_annotated(self.job_id, "source")
        checked = fitz.open(stream=exported, filetype="pdf")
        try:
            exported_text = fitz.Rect(*checked[0].get_text("words")[0][:4])
            vertices = None
            for annot in checked[0].annots():
                vertices = annot.vertices
                break
            self.assertIsNotNone(vertices)
            vertex_center = (sum(point[0] for point in vertices) / len(vertices), sum(point[1] for point in vertices) / len(vertices))
            text_center = ((exported_text.x0 + exported_text.x1) / 2, (exported_text.y0 + exported_text.y1) / 2)
            self.assertLess(abs(vertex_center[0] - text_center[0]), 3)
            self.assertLess(abs(vertex_center[1] - text_center[1]), 3)
        finally:
            checked.close()


if __name__ == "__main__":
    unittest.main()
