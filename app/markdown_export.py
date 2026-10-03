"""Export completed paper jobs to small, linkable Markdown bundles.

This module deliberately has no import-time worker or filesystem side effects.
Call :func:`enqueue_export` from the job completion path and use
:func:`export_job` in tests or one-off repair jobs.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .pdf_worker import pdf_serialized


EXPORT_VERSION = "1"
SCHEMA_VERSION = "2"
CONVERTER_NAME = "PyMuPDF with pypdf fallback"
CONVERTER_VERSION = "1"
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_pending: set[tuple[str, str]] = set()
_pending_lock = threading.Lock()
_queue: queue.Queue[tuple[Path, Path, dict[str, Any]]] | None = None
_worker: threading.Thread | None = None


class MarkdownExportError(RuntimeError):
    """Raised when a Markdown export cannot be safely published."""


def markdown_directory(
    jobs: str | os.PathLike[str],
    job_id: Any,
    job: Mapping[str, Any] | None = None,
) -> Path:
    """Return a verified, completed job's Markdown export directory.

    This is deliberately a read-only guard used by both the HTTP endpoint and
    the native desktop clipboard bridge.  It derives every path from the
    trusted jobs root and the validated job ID; paths in ``job.json`` or the
    Markdown manifest are never followed.
    """
    jobs_root = _as_path(jobs)
    safe_id = _safe_job_id(job_id)
    job_dir = _inside(jobs_root, safe_id)
    if not job_dir.is_dir():
        raise MarkdownExportError("任务目录不存在。")

    if job is None:
        try:
            payload = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise MarkdownExportError("任务不存在。") from exc
        job = payload if isinstance(payload, dict) else None
    if not isinstance(job, Mapping) or str(job.get("id", "")) != safe_id:
        raise MarkdownExportError("任务不存在。")
    if str(job.get("status", "")) != "completed":
        raise MarkdownExportError("任务尚未完成，Markdown 目录尚未生成。")

    try:
        markdown_candidate = job_dir / "markdown"
        if not markdown_candidate.is_dir():
            raise FileNotFoundError(markdown_candidate)
        markdown_dir = markdown_candidate.resolve()
        markdown_dir.relative_to(job_dir.resolve())
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise MarkdownExportError("Markdown 目录尚未生成。") from exc
    if not markdown_dir.is_dir():
        raise MarkdownExportError("Markdown 目录尚未生成。")

    files: dict[str, Path] = {}
    for name in ("source.md", "translated.md", "metadata.json"):
        path = markdown_dir / name
        try:
            if not path.is_file():
                raise FileNotFoundError(path)
            resolved = path.resolve()
            resolved.relative_to(markdown_dir)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise MarkdownExportError("Markdown 导出尚未完整生成。") from exc
        if not resolved.is_file():
            raise MarkdownExportError("Markdown 导出尚未完整生成。")
        files[name] = resolved

    metadata = _read_json(files["metadata.json"])
    if not metadata or str(metadata.get("job_id", "")) != safe_id:
        raise MarkdownExportError("Markdown 元数据无效，目录不可用。")
    expected = {
        "version": EXPORT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "converter": CONVERTER_NAME,
        "converter_version": CONVERTER_VERSION,
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise MarkdownExportError("Markdown 元数据版本不匹配，目录不可用。")
    # Match the cheap completion manifest checks used by job listing.  This
    # reads only small JSON manifests and never hashes or extracts a PDF.
    if "source_sha256" in job and metadata.get("source_sha256") != job.get("source_sha256"):
        raise MarkdownExportError("Markdown 导出与当前源文献不匹配。")
    translation_meta = _read_json(job_dir / "translation-meta.json")
    if translation_meta and metadata.get("translated_sha256") != translation_meta.get("translated_sha256"):
        raise MarkdownExportError("Markdown 导出与当前译文不匹配。")
    if "page_count" in job:
        scope = "demo" if job.get("demo_mode") else str(job.get("translation_scope") or job.get("mode") or "full").lower()
        if scope not in {"full", "trial", "demo"}:
            scope = "full"
        raw_pages = str(job.get("pages") or "").strip()
        try:
            if scope == "full" or not raw_pages:
                selected = list(range(1, int(job.get("page_count") or 0) + 1))
            else:
                selected = []
                for part in raw_pages.split(","):
                    bits = part.strip().split("-", 1)
                    first = int(bits[0])
                    last = int(bits[1]) if len(bits) == 2 and bits[1] else first
                    selected.extend(range(first, last + 1))
                selected = sorted(set(selected))
        except (TypeError, ValueError):
            raise MarkdownExportError("任务页码范围无效，Markdown 目录不可用。")
        if metadata.get("scope") != scope or metadata.get("selected_pages") != selected:
            raise MarkdownExportError("Markdown 导出范围与当前任务不匹配。")

    # Manifest PDF references are metadata only, but reject absolute or
    # escaping values so a malformed manifest can never become a path source.
    for key in ("source_pdf", "translated_pdf"):
        value = metadata.get(key)
        if value is not None:
            try:
                if not isinstance(value, str) or Path(value).is_absolute():
                    raise ValueError(value)
                Path(markdown_dir / value).resolve().relative_to(job_dir.resolve())
            except (OSError, ValueError, TypeError) as exc:
                raise MarkdownExportError("Markdown 元数据包含越界路径。") from exc
    return markdown_dir


def _as_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def _safe_job_id(value: Any) -> str:
    job_id = str(value or "")
    if not _JOB_ID.fullmatch(job_id) or job_id in {".", ".."}:
        raise MarkdownExportError("任务 ID 不安全，拒绝导出。")
    return job_id


def _inside(root: Path, value: str | os.PathLike[str]) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise MarkdownExportError("导出文件路径必须是任务目录内的相对路径。")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise MarkdownExportError("导出文件路径越过任务目录边界。") from exc
    return resolved


def _job_dir(jobs: Path, job: Mapping[str, Any]) -> Path:
    return _inside(jobs, _safe_job_id(job.get("id")))


def _relative_file(job_dir: Path, value: Any, default: str | None = None) -> Path | None:
    if value in (None, ""):
        value = default
    if value in (None, ""):
        return None
    return _inside(job_dir, str(value))


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _page_numbers(job: Mapping[str, Any], page_count: int) -> list[int]:
    scope = str(job.get("translation_scope") or job.get("mode") or "full").lower()
    raw = str(job.get("pages") or "").strip()
    if scope == "full" or not raw:
        return list(range(1, max(0, page_count) + 1))
    numbers: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split("-", 1)
        try:
            start = int(bits[0])
            end = int(bits[1]) if len(bits) == 2 and bits[1] else start
        except ValueError as exc:
            raise MarkdownExportError(f"页码范围无效：{raw}") from exc
        if start < 1 or end < start or end > page_count:
            raise MarkdownExportError(f"页码超出范围：{raw}")
        numbers.update(range(start, end + 1))
    return sorted(numbers)


@pdf_serialized
def _pdf_page_count(path: Path) -> int:
    try:
        import fitz  # type: ignore

        document = fitz.open(path)
        try:
            return int(document.page_count)
        finally:
            document.close()
    except Exception:
        try:
            from pypdf import PdfReader

            return len(PdfReader(str(path)).pages)
        except Exception as exc:
            raise MarkdownExportError(f"无法读取 PDF 页数：{path.name}") from exc


@pdf_serialized
def _extract_pages(path: Path, pages: list[int]) -> tuple[dict[int, str], str]:
    """Extract page text in reading order, preferring PyMuPDF."""
    if not path.is_file():
        return {}, "PDF 文件不存在，无法提取正文。"
    try:
        import fitz  # type: ignore

        document = fitz.open(path)
        try:
            result: dict[int, str] = {}
            for number in pages:
                if number < 1 or number > document.page_count:
                    continue
                page = document.load_page(number - 1)
                blocks = page.get_text("blocks", sort=True)
                chunks = []
                for block in blocks:
                    text = str(block[4] if len(block) > 4 else "").strip()
                    if text:
                        chunks.append(text)
                result[number] = "\n\n".join(chunks).strip()
            return result, "PyMuPDF"
        finally:
            document.close()
    except Exception:
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            result = {}
            for number in pages:
                if number < 1 or number > len(reader.pages):
                    continue
                result[number] = (reader.pages[number - 1].extract_text() or "").strip()
            return result, "pypdf fallback"
        except Exception as exc:
            raise MarkdownExportError(f"无法提取 PDF 正文：{path.name}") from exc


def _clean_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _markdown_for_pages(*, pages: list[int], extracted: Mapping[int, str], pdf_path: Path | None, markdown_dir: Path, label: str) -> str:
    link = None
    if pdf_path and pdf_path.is_file():
        link = Path(os.path.relpath(pdf_path, markdown_dir)).as_posix()
    lines = [f"# {label}", "", "<!-- Generated by paper2zh; page anchors are stable for this export. -->", ""]
    for number in pages:
        anchor = f"{label.lower()}-page-{number}"
        lines.append(f'<a id="{anchor}"></a>')
        if link:
            lines.append(f"## 第 {number} 页 · [打开 PDF](<{link}#page={number}>)")
        else:
            lines.append(f"## 第 {number} 页")
        text = _clean_text(extracted.get(number, ""))
        if text:
            lines.extend(["", text])
        else:
            lines.extend(["", "> [正文提取不可用：本页没有可提取文字，可能是扫描页。]"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _scope(job: Mapping[str, Any]) -> str:
    if job.get("demo_mode"):
        return "demo"
    value = str(job.get("translation_scope") or job.get("mode") or "full").lower()
    return value if value in {"full", "trial", "demo"} else "full"


def _metadata(*, job: Mapping[str, Any], source: Path, translated: Path | None, source_rel: str, translated_rel: str | None, selected_pages: list[int], scope: str, source_hash: str | None, translated_hash: str | None) -> dict[str, Any]:
    return {
        "version": EXPORT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "job_id": str(job["id"]),
        "title": str(job.get("translated_title") or job.get("display_title") or job.get("filename") or "未命名论文"),
        "source_pdf": source_rel,
        "translated_pdf": translated_rel,
        "source_sha256": source_hash,
        "translated_sha256": translated_hash,
        "scope": scope,
        "selected_pages": selected_pages,
        "converter": CONVERTER_NAME,
        "converter_version": CONVERTER_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limitations": [
            "公式和表格按可提取文字保留，不伪造 LaTeX。",
            "扫描页或空文本页会明确标记为正文提取不可用。",
        ],
    }


def _fingerprint(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {key: metadata.get(key) for key in ("version", "schema_version", "converter", "converter_version", "source_sha256", "translated_sha256", "scope", "selected_pages", "source_pdf", "translated_pdf")}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _can_reuse(target: Path, metadata: Mapping[str, Any]) -> bool:
    existing = _read_json(target / "metadata.json")
    return bool(existing and _fingerprint(existing) == _fingerprint(metadata) and all((target / name).is_file() for name in ("source.md", "translated.md", "metadata.json")))


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_publish(directory: Path, target: Path) -> None:
    backup: Path | None = None
    published = False
    if target.exists():
        backup = target.with_name(f".{target.name}.{uuid.uuid4().hex}.old")
        os.replace(target, backup)
    try:
        os.replace(directory, target)
        published = True
    except Exception:
        if backup and backup.exists() and not target.exists():
            try:
                os.replace(backup, target)
            except Exception as restore_error:
                raise MarkdownExportError(f"Markdown 发布失败，旧版本保留于本地备份：{backup}") from restore_error
        raise
    if published and backup and backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


def _library_paths(data: Path) -> tuple[Path, Path]:
    return _inside(data, "index.json"), _inside(data, "README.md")


def _refresh_library_index(data: Path, jobs: Path) -> None:
    index_path, readme_path = _library_paths(data)
    entries = []
    if jobs.is_dir():
        for job_dir in sorted(jobs.iterdir(), key=lambda item: item.name):
            markdown = job_dir / "markdown"
            metadata = _read_json(markdown / "metadata.json")
            if not metadata or not all((markdown / name).is_file() for name in ("source.md", "translated.md")):
                continue
            entries.append({"id": metadata.get("job_id", job_dir.name), "title": metadata.get("title", "未命名论文"), "source": f"jobs/{job_dir.name}/markdown/source.md", "translated": f"jobs/{job_dir.name}/markdown/translated.md", "metadata": f"jobs/{job_dir.name}/markdown/metadata.json"})
    index = {"version": EXPORT_VERSION, "entries": entries}
    index_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(index_path, json.dumps(index, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(readme_path, "# paper2zh Markdown 文献库\n\n`index.json` 按固定任务 ID 列出可用的原文、译文和元数据 Markdown 导出。\n\n导出文件由系统生成，正文仅作为资料阅读。\n")


def export_job(data: str | os.PathLike[str], jobs: str | os.PathLike[str], job: Mapping[str, Any]) -> dict[str, Any]:
    """Synchronously export one completed job and return its metadata/result."""
    data_root, jobs_root = _as_path(data), _as_path(jobs)
    job_id = _safe_job_id(job.get("id"))
    job_dir = _job_dir(jobs_root, job)
    if not job_dir.is_dir():
        raise MarkdownExportError("任务目录不存在，无法导出。")
    source = _relative_file(job_dir, job.get("source_file"), "source.pdf")
    translated = _relative_file(job_dir, job.get("translated_file"))
    if source is None or not source.is_file():
        raise MarkdownExportError("源 PDF 不存在，无法导出。")
    page_count = _pdf_page_count(source)
    selected = _page_numbers(job, page_count)
    scope = _scope(job)
    source_hash, translated_hash = _sha256(source), _sha256(translated) if translated else None
    source_rel = Path(os.path.relpath(source, job_dir / "markdown")).as_posix()
    translated_rel = Path(os.path.relpath(translated, job_dir / "markdown")).as_posix() if translated and translated.is_file() else None
    target = job_dir / "markdown"
    cached_metadata = _metadata(job=job, source=source, translated=translated if translated and translated.is_file() else None, source_rel=source_rel, translated_rel=translated_rel, selected_pages=selected, scope=scope, source_hash=source_hash, translated_hash=translated_hash)
    if _can_reuse(target, cached_metadata):
        _refresh_library_index(data_root, jobs_root)
        return {"status": "reused", "job_id": job_id, "path": str(target), "metadata": _read_json(target / "metadata.json") or cached_metadata}
    all_pages = list(range(1, page_count + 1))
    source_text, _source_converter = _extract_pages(source, all_pages)
    translated_text: dict[int, str] = {}
    if translated and translated.is_file():
        translated_text, _translated_converter = _extract_pages(translated, selected)
    metadata = _metadata(job=job, source=source, translated=translated if translated and translated.is_file() else None, source_rel=source_rel, translated_rel=translated_rel, selected_pages=selected, scope=scope, source_hash=source_hash, translated_hash=translated_hash)
    temporary = Path(tempfile.mkdtemp(prefix=f".{job_id}.markdown-", dir=str(job_dir)))
    try:
        (temporary / "source.md").write_text(_markdown_for_pages(pages=all_pages, extracted=source_text, pdf_path=source, markdown_dir=temporary, label="Source"), encoding="utf-8", newline="\n")
        (temporary / "translated.md").write_text(_markdown_for_pages(pages=selected, extracted=translated_text, pdf_path=translated if translated and translated.is_file() else None, markdown_dir=temporary, label="Translated"), encoding="utf-8", newline="\n")
        (temporary / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        _atomic_publish(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    _refresh_library_index(data_root, jobs_root)
    return {"status": "exported", "job_id": job_id, "path": str(target), "metadata": metadata}


def _worker_loop() -> None:
    global _queue
    assert _queue is not None
    while True:
        item = _queue.get()
        if item is None:  # pragma: no cover - reserved for process shutdown
            return
        data, jobs, job = item
        key = (str(jobs), str(job.get("id", "")))
        try:
            export_job(data, jobs, job)
        except Exception:
            pass
        finally:
            with _pending_lock:
                _pending.discard(key)
            _queue.task_done()


def enqueue_export(data: str | os.PathLike[str], jobs: str | os.PathLike[str], job: Mapping[str, Any]) -> bool:
    """Queue one export; returns False when the same job is already pending."""
    global _queue, _worker
    data_root, jobs_root = _as_path(data), _as_path(jobs)
    job_copy = dict(job)
    job_id = _safe_job_id(job_copy.get("id"))
    key = (str(jobs_root), job_id)
    with _pending_lock:
        if key in _pending:
            return False
        _pending.add(key)
        if _queue is None:
            _queue = queue.Queue()
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop, name="paper2zh-markdown-export", daemon=True)
            _worker.start()
        _queue.put((data_root, jobs_root, job_copy))
    return True


def enqueue_completed_exports(data: str | os.PathLike[str], jobs: str | os.PathLike[str], records: Iterable[Mapping[str, Any]] | None = None) -> int:
    """Queue completed jobs, including legacy jobs discovered at startup."""
    jobs_root = _as_path(jobs)
    if records is None:
        found = []
        for job_dir in jobs_root.iterdir() if jobs_root.is_dir() else []:
            payload = _read_json(job_dir / "job.json") if job_dir.is_dir() else None
            if payload:
                found.append(payload)
        records = found
    count = 0
    for job in records:
        if str(job.get("status")) == "completed" and enqueue_export(data, jobs, job):
            count += 1
    return count


__all__ = ["MarkdownExportError", "enqueue_completed_exports", "enqueue_export", "export_job", "markdown_directory"]
