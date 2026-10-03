"""Local folder metadata for organizing paper jobs without moving PDFs."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from . import core

MAX_LIBRARY_BODY_BYTES = 64 * 1024
MAX_FOLDER_NAME_LENGTH = 100
_lock = threading.RLock()


def _library_path() -> Path:
    return core.DATA / "library.json"


def _empty() -> dict[str, Any]:
    return {"folders": [], "documents": {}}


def _read() -> dict[str, Any]:
    try:
        payload = json.loads(_library_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return _empty()
    if not isinstance(payload, dict):
        return _empty()
    folders = payload.get("folders", [])
    documents = payload.get("documents", {})
    if not isinstance(folders, list) or not isinstance(documents, dict):
        return _empty()
    return {
        "folders": [item for item in folders if isinstance(item, dict) and isinstance(item.get("id"), str)],
        "documents": {str(job_id): folder_id for job_id, folder_id in documents.items() if isinstance(job_id, str) and (folder_id is None or isinstance(folder_id, str))},
    }


def _write(payload: dict[str, Any]) -> None:
    destination = _library_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _folder_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in payload["folders"]}


def _name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("文件夹名称必须是文本。")
    value = value.strip()
    if not value or len(value) > MAX_FOLDER_NAME_LENGTH or "\x00" in value or any(ord(char) < 32 for char in value):
        raise ValueError(f"文件夹名称不能为空，且长度不能超过 {MAX_FOLDER_NAME_LENGTH} 个字符。")
    return value


def _parent(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("parent_id 必须是文件夹 id 或 null。")
    return value


def _check_parent(folder_id: str | None, parent_id: str | None, folders: dict[str, dict[str, Any]]) -> None:
    if parent_id is None:
        return
    if parent_id not in folders:
        raise ValueError("父文件夹不存在。")
    if folder_id is None:
        return
    seen: set[str] = set()
    current: str | None = parent_id
    while current is not None:
        if current == folder_id:
            raise ValueError("不能把文件夹移动到自身或其子文件夹中。")
        if current in seen:
            raise ValueError("文件夹层级已形成循环。")
        seen.add(current)
        current = folders[current].get("parent_id")


def _check_job(job_id: Any) -> str:
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", job_id):
        raise ValueError("job_id 无效。")
    candidate = (core.JOBS / job_id).resolve()
    try:
        candidate.relative_to(core.JOBS.resolve())
    except ValueError as exc:
        raise ValueError("job_id 无效。") from exc
    if core.load_job(job_id) is None:
        raise ValueError("文献任务不存在。")
    return job_id


def get_library() -> dict[str, Any]:
    with _lock:
        payload = _read()
        # Keep the public shape stable and omit incidental metadata.
        folders = [{"id": item["id"], "name": item.get("name", ""), "parent_id": item.get("parent_id")} for item in payload["folders"]]
        return {"folders": folders, "documents": dict(payload["documents"])}


def apply_action(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("文件库请求格式无效。")
    action = payload.get("action")
    with _lock:
        state = _read()
        folders = _folder_map(state)
        if action == "create_folder":
            name = _name(payload.get("name"))
            parent_id = _parent(payload.get("parent_id"))
            _check_parent(None, parent_id, folders)
            folder = {"id": uuid.uuid4().hex, "name": name, "parent_id": parent_id}
            state["folders"].append(folder)
        elif action == "rename_folder":
            folder_id = payload.get("id")
            if not isinstance(folder_id, str) or folder_id not in folders:
                raise ValueError("文件夹不存在。")
            folders[folder_id]["name"] = _name(payload.get("name"))
        elif action == "move_folder":
            folder_id = payload.get("id")
            if not isinstance(folder_id, str) or folder_id not in folders:
                raise ValueError("文件夹不存在。")
            parent_id = _parent(payload.get("parent_id"))
            _check_parent(folder_id, parent_id, folders)
            folders[folder_id]["parent_id"] = parent_id
        elif action == "move_document":
            job_id = _check_job(payload.get("job_id"))
            folder_id = _parent(payload.get("folder_id"))
            if folder_id is not None and folder_id not in folders:
                raise ValueError("目标文件夹不存在。")
            state["documents"][job_id] = folder_id
        elif action == "delete_document":
            job_id = _check_job(payload.get("job_id"))
            previous = json.loads(json.dumps(state, ensure_ascii=False))
            state["documents"].pop(job_id, None)
            # Publish the association change first. If the owned bundle
            # deletion is refused, restore the previous library mapping.
            _write(state)
            try:
                core.delete_job(job_id)
            except Exception:
                try:
                    _write(previous)
                except Exception as restore_exc:
                    raise OSError(f"删除失败，且无法恢复文献库关联：{restore_exc}")
                raise
            return get_library()
        else:
            raise ValueError("action 必须是 create_folder、rename_folder、move_folder、delete_document。")
        _write(state)
        return get_library()
