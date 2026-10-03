"""Persistent PDF.js-coordinate annotations for paper jobs.

Annotations are keyed by the SHA-256 revision of the selected PDF.  This
keeps notes for an older translated PDF available without applying them to a
newly generated file.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from . import core
from .pdf_worker import pdf_serialized

ANNOTATION_TYPES = {"highlight", "underline"}
ANNOTATION_COLORS = {"#FFE066", "#8CE99A", "#74C0FC", "#FAA2C1"}
MAX_RECTS = 500
MAX_ANNOTATION_BODY_BYTES = 512 * 1024
_lock = threading.RLock()


class RevisionMismatch(ValueError):
    """The client edited a PDF revision different from the current one."""

    def __init__(self, current_revision: str):
        super().__init__("PDF 版本已变化，请重新加载批注后再操作。")
        self.current_revision = current_revision


class AnnotationNotFound(ValueError):
    pass


def _fitz():
    runtime = core.ROOT / ".runtime"
    if runtime.is_dir() and str(runtime) not in os.sys.path:
        os.sys.path.insert(0, str(runtime))
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError("缺少 PyMuPDF，无法处理 PDF 批注。") from exc
    return fitz


def _annotation_path(job_id: str) -> Path:
    # job_id is validated through core.file_path before this function is used.
    return core.JOBS / job_id / "annotations.json"


def _group_key(side: str, revision: str) -> str:
    return f"{side}:{revision}"


def _read_groups(job_id: str) -> dict[str, list[dict[str, Any]]]:
    try:
        payload = json.loads(_annotation_path(job_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(key, str) and isinstance(value, list)}


def _write_groups(job_id: str, groups: dict[str, list[dict[str, Any]]]) -> None:
    destination = _annotation_path(job_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(groups, ensure_ascii=False, indent=2, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _pdf_path(job_id: str, side: str) -> Path:
    if side not in {"source", "translated"}:
        raise ValueError("只支持 source 或 translated 批注。")
    path = core.file_path(job_id, side)
    if path is None:
        raise FileNotFoundError("任务或对应 PDF 不存在。")
    try:
        path.resolve().relative_to(core.JOBS.resolve())
    except ValueError as exc:
        raise FileNotFoundError("任务或对应 PDF 不存在。") from exc
    return path


def revision_for(job_id: str, side: str) -> tuple[Path, str]:
    path = _pdf_path(job_id, side)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return path, digest.hexdigest()


@pdf_serialized
def _page_bounds(pdf_path: Path, page_number: int) -> tuple[float, float, float, float]:
    fitz = _fitz()
    document = fitz.open(str(pdf_path))
    try:
        if page_number < 1 or page_number > len(document):
            raise ValueError("批注页码超出范围。")
        page = document.load_page(page_number - 1)
        # PDF.js convertToPdfPoint returns PDF user-space coordinates.  Use the
        # visible crop box, including a non-zero origin, instead of page.rect
        # (which is display-rotated on rotated pages).
        box = page.cropbox
        return float(box.x0), float(box.y0), float(box.x1), float(box.y1)
    finally:
        document.close()


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{label} 必须是有限数字。")
    return float(value)


def _normalise_rects(value: Any, page: int, pdf_path: Path) -> list[list[float]]:
    if not isinstance(value, list) or not value or len(value) > MAX_RECTS:
        raise ValueError(f"rects 必须包含 1-{MAX_RECTS} 个矩形。")
    left, bottom, right, top = _page_bounds(pdf_path, page)
    result: list[list[float]] = []
    for index, rect in enumerate(value, 1):
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            raise ValueError(f"第 {index} 个矩形必须是 [x0,y0,x1,y1]。")
        x0, y0, x1, y1 = (_number(item, f"第 {index} 个矩形坐标") for item in rect)
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        if x0 >= x1 or y0 >= y1 or x0 < left or y0 < bottom or x1 > right or y1 > top:
            raise ValueError(f"第 {index} 个矩形超出第 {page} 页范围。")
        result.append([x0, y0, x1, y1])
    return result


def _current_group(job_id: str, side: str, supplied_revision: str) -> tuple[Path, str, str, dict[str, list[dict[str, Any]]]]:
    pdf_path, current_revision = revision_for(job_id, side)
    if supplied_revision != current_revision:
        raise RevisionMismatch(current_revision)
    return pdf_path, current_revision, _group_key(side, current_revision), _read_groups(job_id)


def get_annotations(job_id: str, side: str) -> dict[str, Any]:
    _pdf_path_value, revision = revision_for(job_id, side)
    with _lock:
        groups = _read_groups(job_id)
        return {"revision": revision, "annotations": list(groups.get(_group_key(side, revision), []))}


def add_annotation(job_id: str, side: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("批注请求格式无效。")
    supplied_revision = payload.get("revision")
    if not isinstance(supplied_revision, str) or len(supplied_revision) != 64:
        raise ValueError("revision 无效。")
    with _lock:
        pdf_path, revision, key, groups = _current_group(job_id, side, supplied_revision)
        annotation_type = payload.get("type")
        if annotation_type not in ANNOTATION_TYPES:
            raise ValueError("批注类型必须是 highlight 或 underline。")
        color = str(payload.get("color", "")).upper()
        if color not in ANNOTATION_COLORS:
            raise ValueError("批注颜色不在允许列表中。")
        page = payload.get("page")
        if isinstance(page, bool) or not isinstance(page, int):
            raise ValueError("批注页码必须是整数。")
        rects = _normalise_rects(payload.get("rects"), page, pdf_path)
        annotation = {"id": uuid.uuid4().hex, "page": page, "type": annotation_type, "color": color, "rects": rects}
        groups[key] = list(groups.get(key, [])) + [annotation]
        _write_groups(job_id, groups)
        return {"revision": revision, "annotations": groups[key]}


def delete_annotation(job_id: str, side: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("批注请求格式无效。")
    supplied_revision = payload.get("revision")
    annotation_id = payload.get("id")
    if not isinstance(supplied_revision, str) or len(supplied_revision) != 64:
        raise ValueError("revision 无效。")
    if not isinstance(annotation_id, str) or not annotation_id:
        raise ValueError("批注 id 无效。")
    with _lock:
        _pdf_path_value, revision, key, groups = _current_group(job_id, side, supplied_revision)
        annotations = list(groups.get(key, []))
        remaining = [item for item in annotations if item.get("id") != annotation_id]
        if len(remaining) == len(annotations):
            raise AnnotationNotFound("批注不存在。")
        groups[key] = remaining
        _write_groups(job_id, groups)
        return {"revision": revision, "annotations": remaining}


def _rgb(color: str) -> tuple[float, float, float]:
    return tuple(int(color[index:index + 2], 16) / 255 for index in (1, 3, 5))  # type: ignore[return-value]


def _to_mupdf_rect(page: Any, rect: list[float]) -> Any:
    fitz = _fitz()
    # PDF.js gives PDF-space coordinates (bottom-left origin).  PyMuPDF's
    # transformation matrix performs the y-axis conversion and page offset.
    return (fitz.Rect(*rect) * page.transformation_matrix).normalize()


def export_annotated(job_id: str, side: str) -> tuple[bytes, str]:
    pdf_path, revision = revision_for(job_id, side)
    with _lock:
        groups = _read_groups(job_id)
        annotations = list(groups.get(_group_key(side, revision), []))
    return _export_annotated_pdf(pdf_path, annotations, revision)


@pdf_serialized
def _export_annotated_pdf(pdf_path: Path, annotations: list[dict[str, Any]], revision: str) -> tuple[bytes, str]:
    fitz = _fitz()
    document = fitz.open(str(pdf_path))
    try:
        for item in annotations:
            page_number = int(item["page"])
            if page_number < 1 or page_number > len(document):
                continue
            page = document.load_page(page_number - 1)
            color = _rgb(item["color"])
            for rect in item["rects"]:
                mupdf_rect = _to_mupdf_rect(page, rect)
                if item["type"] == "underline":
                    annot = page.add_underline_annot(mupdf_rect)
                else:
                    annot = page.add_highlight_annot(mupdf_rect)
                annot.set_colors(stroke=color)
                annot.update()
        return document.tobytes(garbage=4, deflate=True), revision
    finally:
        document.close()
