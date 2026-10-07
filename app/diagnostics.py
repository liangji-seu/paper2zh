"""Small, local-only diagnostic log for paper2zh.

The diagnostic stream is deliberately separate from BabelDOC's engine log.  It
contains event names and a small set of safe, structured values, never an
exception message or request contents.  Every public helper is best effort so
that a read-only diagnostic feature can never make the application fail.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

LOG_NAME = "diagnostics.log"
MAX_LOG_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3
MAX_CLIENT_ERROR_BYTES = 4 * 1024
_lock = threading.RLock()
_data_dir: Path | None = None
_hooks_installed = False

_SAFE_KEYS = {
    "category", "encoding", "event_source", "http_status", "method", "page",
    "pdf", "phase", "route", "status", "worker", "count", "bytes",
}
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:/-]{1,96}$")
_SAFE_JOB = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_PDF = {"source", "translated", "bilingual"}
_CLIENT_EVENTS = {"pdf_preview_failed", "pdf_annotation_failed"}


def _version() -> str:
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()[:40] or "unknown"
    except Exception:
        return "unknown"


def configure(data_dir: str | os.PathLike[str] | None = None) -> Path:
    """Select the log directory and create it, returning the directory path."""
    global _data_dir
    if data_dir is None:
        configured = os.environ.get("PAPER_TRANSLATOR_DATA_DIR", "").strip()
        data_dir = Path(configured).expanduser() if configured else Path(__file__).resolve().parent.parent / "data"
    root = Path(data_dir).expanduser()
    with _lock:
        _data_dir = root / "logs"
        try:
            _data_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            # Keep the path for a later retry, but do not make startup fail.
            pass
        return _data_dir


def log_directory() -> Path:
    with _lock:
        return _data_dir or configure()


def _safe_value(key: str, value: Any) -> Any:
    if key == "page" or key == "http_status" or key == "count" or key == "bytes":
        if isinstance(value, bool):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        if key == "page" and not 1 <= number <= 100000:
            return None
        if key == "http_status" and not 100 <= number <= 599:
            return None
        if key in {"count", "bytes"} and not 0 <= number <= 2**63 - 1:
            return None
        return number
    if key == "pdf":
        return value if value in _SAFE_PDF else None
    if isinstance(value, str) and _SAFE_TEXT.fullmatch(value):
        return value
    return None


def _safe_fields(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not isinstance(fields, Mapping):
        return result
    for key in _SAFE_KEYS:
        value = _safe_value(key, fields.get(key))
        if value is not None:
            result[key] = value
    return result


def _safe_job_id(job_id: Any) -> str | None:
    return job_id if isinstance(job_id, str) and _SAFE_JOB.fullmatch(job_id) else None


def _stack(exc: BaseException | None) -> list[dict[str, Any]]:
    if exc is None:
        return []
    try:
        frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
        return [{"file": Path(frame.filename).name, "function": frame.name, "line": int(frame.lineno)} for frame in frames[-12:]]
    except Exception:
        return []


def record(event: str, *, job_id: str | None = None, fields: Mapping[str, Any] | None = None,
           exc: BaseException | None = None) -> None:
    """Append one safe JSONL event.  Any logging failure is swallowed."""
    try:
        if not isinstance(event, str) or not _SAFE_TEXT.fullmatch(event):
            event = "invalid_event"
        item: dict[str, Any] = {
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "version": _version(),
            "pid": os.getpid(),
            "thread": threading.current_thread().name[:80],
            "event": event,
        }
        safe_job = _safe_job_id(job_id)
        if safe_job:
            item["job_id"] = safe_job
        item.update(_safe_fields(fields))
        if exc is not None:
            item["exception"] = type(exc).__name__[:120]
            item["stack"] = _stack(exc)
        payload = (json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        directory = log_directory()
        with _lock:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / LOG_NAME
            if path.exists() and path.stat().st_size + len(payload) > MAX_LOG_BYTES:
                for index in range(BACKUP_COUNT, 0, -1):
                    source = directory / f"{LOG_NAME}.{index}"
                    destination = directory / f"{LOG_NAME}.{index + 1}"
                    if source.exists():
                        if index == BACKUP_COUNT:
                            source.unlink(missing_ok=True)
                        else:
                            os.replace(source, destination)
                if path.exists():
                    os.replace(path, directory / f"{LOG_NAME}.1")
            with path.open("ab") as stream:
                stream.write(payload)
    except Exception:
        return


def record_exception(event: str, exc: BaseException, *, job_id: str | None = None,
                     fields: Mapping[str, Any] | None = None) -> None:
    record(event, job_id=job_id, fields=fields, exc=exc)


def _hook(event: str, exc_type: type[BaseException], exc_value: BaseException | None, tb: Any = None) -> None:
    if exc_value is None:
        return
    record_exception(event, exc_value)


def install_exception_hooks() -> None:
    """Install process and thread hooks once, retaining the original hooks."""
    global _hooks_installed
    with _lock:
        if _hooks_installed:
            return
        previous_sys = sys.excepthook
        previous_thread = getattr(threading, "excepthook", None)

        def sys_hook(exc_type: type[BaseException], exc_value: BaseException, tb: Any) -> None:
            _hook("uncaught_exception", exc_type, exc_value, tb)
            try:
                previous_sys(exc_type, exc_value, tb)
            except Exception:
                pass

        sys.excepthook = sys_hook
        if previous_thread is not None:
            def thread_hook(args: Any) -> None:
                _hook("uncaught_thread_exception", args.exc_type, args.exc_value, args.exc_traceback)
                try:
                    previous_thread(args)
                except Exception:
                    pass
            threading.excepthook = thread_hook
        _hooks_installed = True


def client_error(payload: Any) -> dict[str, Any]:
    """Validate a browser diagnostic payload and record only its safe fields."""
    if not isinstance(payload, Mapping):
        raise ValueError("诊断上报格式无效。")
    allowed = {"event", "pdf", "page", "job_id"}
    values = {key: payload[key] for key in allowed if key in payload}
    event = values.get("event")
    if event not in _CLIENT_EVENTS:
        raise ValueError("诊断事件无效。")
    side = values.get("pdf")
    if side is not None and side not in _SAFE_PDF:
        raise ValueError("诊断 PDF 侧别无效。")
    page = values.get("page")
    if page is not None and (isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 100000):
        raise ValueError("诊断页码无效。")
    job_id = values.get("job_id")
    if job_id is not None and _safe_job_id(job_id) is None:
        raise ValueError("诊断任务 ID 无效。")
    fields = {key: values[key] for key in ("pdf", "page") if key in values}
    record(event, job_id=job_id, fields=fields)
    return {"ok": True}


def export_bytes() -> bytes:
    """Build an in-memory ZIP containing only diagnostics and version summary."""
    stream = io.BytesIO()
    try:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("version.json", json.dumps({"version": _version()}, ensure_ascii=False, indent=2) + "\n")
            directory = log_directory()
            for path in sorted(directory.glob(f"{LOG_NAME}*")):
                if path.is_file() and path.name in {LOG_NAME, *(f"{LOG_NAME}.{n}" for n in range(1, BACKUP_COUNT + 1))}:
                    try:
                        archive.writestr(path.name, path.read_bytes())
                    except OSError:
                        continue
        return stream.getvalue()
    except Exception:
        # A valid, empty archive remains useful and keeps export best effort.
        try:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("version.json", json.dumps({"version": _version()}))
            return stream.getvalue()
        except Exception:
            return b""
