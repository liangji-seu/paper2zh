"""Small SQLite index for the private on-disk paper library.

The JSON job files remain the source of truth for backwards compatibility.
This index only accelerates duplicate detection and library listing; it never
stores API settings or absolute paths supplied by a native file picker.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_lock = threading.RLock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    job_id TEXT PRIMARY KEY,
    source_sha256 TEXT NOT NULL,
    filename TEXT NOT NULL,
    display_title TEXT NOT NULL,
    translated_title TEXT,
    created_at TEXT NOT NULL,
    source_pdf TEXT NOT NULL DEFAULT 'source.pdf',
    translated_pdf TEXT,
    bilingual_pdf TEXT,
    source_md TEXT,
    translated_md TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS documents_created_idx ON documents(created_at DESC);
CREATE INDEX IF NOT EXISTS documents_hash_idx ON documents(source_sha256);
CREATE TABLE IF NOT EXISTS source_index (
    source_sha256 TEXT PRIMARY KEY,
    job_id TEXT NOT NULL
);
"""


def _db(data: Path) -> Path:
    return data / "catalog.db"


def _connect(data: Path) -> sqlite3.Connection:
    data.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(_db(data)), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    # Upgrade databases created by the first catalog implementation without
    # touching the JSON job source of truth.
    for column, definition in {
        "status": "TEXT",
        "mode": "TEXT",
        "page_count": "INTEGER",
        "error": "TEXT",
        "demo_mode": "INTEGER",
        "translated_sha256": "TEXT",
        "translation_scope": "TEXT",
        "pages": "TEXT",
    }.items():
        try:
            connection.execute(f"ALTER TABLE documents ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            pass
    return connection


def _translation_sha(data: Path, job: dict[str, Any]) -> str | None:
    try:
        payload = json.loads((data / "jobs" / str(job.get("id")) / "translation-meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = payload.get("translated_sha256") if isinstance(payload, dict) else None
    return str(value) if value else None


def _row_job(row: sqlite3.Row | None) -> str | None:
    return str(row["job_id"]) if row else None


def claim(data: Path, source_sha256: str, job_id: str, *, filename: str, created_at: str) -> str | None:
    """Atomically reserve a new source hash, returning an existing job id."""
    with _lock:
        connection = _connect(data)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT job_id FROM source_index WHERE source_sha256 = ?", (source_sha256,)).fetchone()
            if row:
                existing_dir = data / "jobs" / str(row["job_id"])
                if not (existing_dir / "job.json").is_file():
                    pending = connection.execute("SELECT status, created_at FROM documents WHERE job_id = ?", (str(row["job_id"]),)).fetchone()
                    active = False
                    if pending and pending["status"] == "importing":
                        try:
                            active = time.time() - datetime.fromisoformat(str(pending["created_at"])).timestamp() < 30
                        except (TypeError, ValueError, OSError):
                            active = False
                    if active:
                        connection.commit()
                        return _row_job(row)
                    connection.execute("DELETE FROM source_index WHERE source_sha256 = ?", (source_sha256,))
                    connection.execute("DELETE FROM documents WHERE job_id = ?", (str(row["job_id"]),))
                    row = None
            if row:
                connection.commit()
                return _row_job(row)
            connection.execute(
                "INSERT INTO documents(job_id, source_sha256, filename, display_title, created_at, status, mode, error, demo_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, source_sha256, filename, filename, created_at, "importing", "full", "", 0),
            )
            connection.execute("INSERT INTO source_index(source_sha256, job_id) VALUES (?, ?)", (source_sha256, job_id))
            connection.commit()
            return None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def remove(data: Path, job_id: str) -> None:
    with _lock:
        connection = _connect(data)
        try:
            hashes = [str(row[0]) for row in connection.execute(
                "SELECT DISTINCT source_sha256 FROM documents WHERE job_id = ?",
                (job_id,),
            ).fetchall()]
            connection.execute("DELETE FROM source_index WHERE job_id = ?", (job_id,))
            connection.execute("DELETE FROM documents WHERE job_id = ?", (job_id,))
            # Restore duplicate-source mapping when another legacy job with
            # the same bytes remains in the catalog.
            for source_hash in hashes:
                candidates = connection.execute(
                    "SELECT job_id, status, translated_pdf FROM documents WHERE source_sha256 = ? ORDER BY created_at ASC",
                    (source_hash,),
                ).fetchall()
                preferred = None
                for candidate in candidates:
                    candidate_dir = data / "jobs" / str(candidate["job_id"])
                    translated = candidate["translated_pdf"]
                    if (candidate["status"] == "completed" and isinstance(translated, str)
                            and (candidate_dir / translated).is_file()
                            and (candidate_dir / "job.json").is_file()):
                        preferred = candidate["job_id"]
                        break
                if preferred is None:
                    for candidate in candidates:
                        if (data / "jobs" / str(candidate["job_id"]) / "job.json").is_file():
                            preferred = candidate["job_id"]
                            break
                if preferred is not None:
                    connection.execute(
                        "INSERT OR REPLACE INTO source_index(source_sha256, job_id) VALUES (?, ?)",
                        (source_hash, str(preferred)),
                    )
            connection.commit()
        finally:
            connection.close()


def update(data: Path, job: dict[str, Any]) -> None:
    job_id = str(job.get("id", ""))
    if not job_id:
        return
    source_hash = str(job.get("source_sha256") or "")
    if not source_hash:
        return
    markdown = job.get("markdown") if isinstance(job.get("markdown"), dict) else {}
    metadata = job.get("catalog_metadata") if isinstance(job.get("catalog_metadata"), dict) else {}
    with _lock:
        connection = _connect(data)
        try:
            connection.execute(
                """INSERT INTO documents(job_id, source_sha256, filename, display_title, translated_title,
                   created_at, translated_pdf, bilingual_pdf, source_md, translated_md, metadata_json,
                   status, mode, page_count, error, demo_mode, translated_sha256, translation_scope, pages)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id) DO UPDATE SET source_sha256=excluded.source_sha256,
                   filename=excluded.filename, display_title=excluded.display_title,
                   translated_title=excluded.translated_title, translated_pdf=excluded.translated_pdf,
                   bilingual_pdf=excluded.bilingual_pdf, source_md=excluded.source_md,
                   translated_md=excluded.translated_md, metadata_json=excluded.metadata_json,
                   status=excluded.status, mode=excluded.mode, page_count=excluded.page_count,
                   error=excluded.error, demo_mode=excluded.demo_mode,
                   translated_sha256=excluded.translated_sha256, translation_scope=excluded.translation_scope,
                   pages=excluded.pages""",
                (
                    job_id, source_hash, str(job.get("filename") or "paper.pdf"),
                    str(job.get("display_title") or job.get("filename") or "paper.pdf"),
                    job.get("translated_title"), str(job.get("created_at") or ""),
                    job.get("translated_file"), job.get("bilingual_file"),
                    markdown.get("source"), markdown.get("translated"),
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    str(job.get("status") or ""), str(job.get("mode") or ""),
                    job.get("page_count"), str(job.get("error") or ""), int(bool(job.get("demo_mode"))),
                    _translation_sha(data, job), str(job.get("translation_scope") or ""), str(job.get("pages") or ""),
                ),
            )
            connection.execute("INSERT OR IGNORE INTO source_index(source_sha256, job_id) VALUES (?, ?)", (source_hash, job_id))
            connection.commit()
        finally:
            connection.close()


def sync(data: Path, jobs: Path) -> None:
    """Index old JSON jobs once without removing duplicate legacy records."""
    if not jobs.is_dir():
        _connect(data).close()
        return
    connection = _connect(data)
    try:
        for job_path in jobs.glob("*/job.json"):
            try:
                job = json.loads(job_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(job, dict) or not job.get("id"):
                continue
            source_path = job_path.parent / "source.pdf"
            source_hash = str(job.get("source_sha256") or "")
            if not source_hash and source_path.is_file():
                digest = hashlib.sha256()
                with source_path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                source_hash = digest.hexdigest()
            if not source_hash:
                continue
            # INSERT OR IGNORE deliberately keeps all old job rows, while
            # source_index chooses only the first legacy job for new imports.
            connection.execute(
                "INSERT OR IGNORE INTO documents(job_id, source_sha256, filename, display_title, translated_title, created_at, translated_pdf, bilingual_pdf, status, mode, page_count, error, demo_mode, translated_sha256, translation_scope, pages) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(job["id"]), source_hash, str(job.get("filename") or "paper.pdf"), str(job.get("display_title") or job.get("filename") or "paper.pdf"), job.get("translated_title"), str(job.get("created_at") or ""), job.get("translated_file"), job.get("bilingual_file"), str(job.get("status") or ""), str(job.get("mode") or ""), job.get("page_count"), str(job.get("error") or ""), int(bool(job.get("demo_mode"))), None, str(job.get("translation_scope") or ""), str(job.get("pages") or "")),
            )
            connection.execute("INSERT OR IGNORE INTO source_index(source_sha256, job_id) VALUES (?, ?)", (source_hash, str(job["id"])))
        hashes = connection.execute("SELECT DISTINCT source_sha256 FROM documents").fetchall()
        for hash_row in hashes:
            source_hash = str(hash_row[0])
            candidates = connection.execute(
                "SELECT job_id, status, translated_pdf FROM documents WHERE source_sha256 = ? ORDER BY created_at ASC",
                (source_hash,),
            ).fetchall()
            preferred = None
            for candidate in candidates:
                translated = candidate["translated_pdf"]
                if candidate["status"] == "completed" and isinstance(translated, str) and (jobs / str(candidate["job_id"]) / translated).is_file():
                    preferred = candidate["job_id"]
                    break
            preferred = preferred or (candidates[0]["job_id"] if candidates else None)
            if preferred:
                connection.execute("INSERT OR REPLACE INTO source_index(source_sha256, job_id) VALUES (?, ?)", (source_hash, preferred))
        connection.commit()
    finally:
        connection.close()


def list_documents(data: Path) -> list[dict[str, Any]]:
    with _lock:
        connection = _connect(data)
        try:
            rows = connection.execute("SELECT job_id, source_sha256, filename, display_title, translated_title, created_at, translated_pdf, bilingual_pdf, source_md, translated_md, status, mode, page_count, error, demo_mode, translated_sha256, translation_scope, pages FROM documents ORDER BY created_at DESC").fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()
