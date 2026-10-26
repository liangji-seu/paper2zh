"""Per-paper reading memo stored beside the generated Markdown bundle."""

from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import JOBS, load_job

MEMO_FILENAME = "notes.md"
MAX_MEMO_CHARS = 200_000
_lock = threading.RLock()


def _job_dir(job_id: str) -> Path:
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", job_id):
        raise ValueError("文献 ID 无效。")
    job = load_job(job_id)
    if not job:
        raise FileNotFoundError("文献不存在或已被删除。")
    root = (JOBS / job_id).resolve()
    try:
        root.relative_to(JOBS.resolve())
    except ValueError as exc:
        raise ValueError("文献路径不安全。") from exc
    return root


def memo_path(job_id: str) -> Path:
    return _job_dir(job_id) / "markdown" / MEMO_FILENAME


def read_memo(job_id: str) -> dict[str, Any]:
    path = memo_path(job_id)
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"content": "", "updated_at": None}
    except OSError as exc:
        raise OSError(f"备忘录读取失败：{exc}") from exc
    stat = path.stat()
    return {
        "content": content,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def save_memo(job_id: str, content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        raise ValueError("备忘录内容必须是文本。")
    if len(content) > MAX_MEMO_CHARS:
        raise ValueError(f"备忘录过长，请控制在 {MAX_MEMO_CHARS} 字以内。")
    path = memo_path(job_id)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8", newline="\n")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    stat = path.stat()
    return {
        "content": content,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }
