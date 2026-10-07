from __future__ import annotations

import json
import hashlib
import io
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pypdf import PdfReader

from .security import mask, protect, unprotect
from .progress import parse_engine_progress
from . import catalog
from .diagnostics import configure as configure_diagnostics, record as diagnostic, record_exception as diagnostic_exception
from .pdf_worker import pdf_serialized


ROOT = Path(__file__).resolve().parent.parent
_configured_data = os.environ.get("PAPER_TRANSLATOR_DATA_DIR", "").strip()
if _configured_data:
    DATA = Path(_configured_data).expanduser()
elif getattr(sys, "_MEIPASS", None):
    DATA = Path(sys.executable).resolve().parent / "library"
else:
    DATA = ROOT / "data"
configure_diagnostics(DATA)
JOBS = DATA / "jobs"
_configured_settings = os.environ.get("PAPER_TRANSLATOR_SETTINGS_PATH", "").strip()
if _configured_settings:
    SETTINGS_PATH = Path(_configured_settings).expanduser()
elif getattr(sys, "_MEIPASS", None) and os.environ.get("LOCALAPPDATA"):
    SETTINGS_PATH = Path(os.environ["LOCALAPPDATA"]) / "paper2zh" / "settings.json"
else:
    SETTINGS_PATH = DATA / "settings.json"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_LOCAL_IMPORT_BYTES = MAX_UPLOAD_BYTES
EXTERNAL_SOURCE_META_NAME = "external-source.json"

DEFAULT_SETTINGS = {
    "provider": "DeepSeek",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-flash",
    "language_in": "en",
    "language_out": "zh",
}

_lock = threading.RLock()
_jobs: dict[str, dict[str, Any]] = {}
_catalog_synced: set[str] = set()
_migration_lock = threading.RLock()
_sha_cache: dict[tuple[str, int, int, int], str] = {}
_markdown_scheduled: set[str] = set()
_active_jobs: set[str] = set()
_deleted_jobs: set[str] = set()


def ensure_dirs() -> None:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        JOBS.mkdir(parents=True, exist_ok=True)
        _migrate_legacy_data()
        key = str(DATA.resolve())
        if key not in _catalog_synced:
            catalog.sync(DATA, JOBS)
            _catalog_synced.add(key)
    except Exception as exc:
        raise OSError(f"文献库目录不可写：{DATA}（{exc}）") from exc


def _migrate_legacy_data() -> None:
    """Copy the old user data once, retaining the old directory."""
    source_value = os.environ.get("PAPER_TRANSLATOR_LEGACY_DATA_DIR", "").strip()
    if not source_value:
        return
    source = Path(source_value).expanduser()
    if not source.is_dir() or source.resolve() == DATA.resolve():
        return
    marker = DATA / ".migration-v1.complete"
    with _migration_lock:
        if marker.exists():
            return
        try:
            old_jobs = source / "jobs"
            if old_jobs.is_dir():
                for item in old_jobs.iterdir():
                    destination = JOBS / item.name
                    if not destination.exists():
                        temporary = JOBS / f".{item.name}.migration-{uuid.uuid4().hex}.tmp"
                        try:
                            if item.is_dir():
                                shutil.copytree(item, temporary)
                            else:
                                _atomic_copy(item, temporary)
                            os.replace(temporary, destination)
                        finally:
                            if temporary.is_dir():
                                shutil.rmtree(temporary, ignore_errors=True)
                            else:
                                temporary.unlink(missing_ok=True)
            old_library = source / "library.json"
            if old_library.is_file() and not (DATA / "library.json").exists():
                _atomic_copy(old_library, DATA / "library.json")
            marker.write_text("migration complete\n", encoding="utf-8")
        except Exception as exc:
            raise OSError(f"旧文献库迁移失败，原数据已保留，未标记迁移完成：{exc}") from exc


def read_settings() -> dict[str, Any]:
    ensure_dirs()
    try:
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        payload = {}
    settings = dict(DEFAULT_SETTINGS)
    for key in DEFAULT_SETTINGS:
        if isinstance(payload.get(key), str):
            settings[key] = payload[key]
    protected = payload.get("api_key_protected", "")
    try:
        api_key = unprotect(protected)
    except Exception:
        api_key = ""
    settings["api_key"] = api_key
    return settings


def public_settings() -> dict[str, Any]:
    settings = read_settings()
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        protected = raw.get("api_key_protected", "")
    except (FileNotFoundError, json.JSONDecodeError):
        protected = ""
    storage = "Windows DPAPI 加密后保存于本机 data/settings.json" if protected.startswith("dpapi:") else "Windows DPAPI 待设置（当前未保存 API Key）"
    return {**{k: settings[k] for k in DEFAULT_SETTINGS}, "api_key_masked": mask(settings["api_key"]), "has_api_key": bool(settings["api_key"]), "storage": storage}


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    current = read_settings()
    old_endpoint = (current["provider"], current["base_url"])
    new_provider = values.get("provider", current["provider"])
    new_base_url = values.get("base_url", current["base_url"])
    endpoint_changed = (new_provider, new_base_url) != old_endpoint
    replacing_key = isinstance(values.get("api_key"), str) and bool(values["api_key"].strip())
    if endpoint_changed and current["api_key"] and not replacing_key and not values.get("clear_api_key") and not values.get("reuse_existing_key"):
        raise ValueError("Provider 或 Base URL 已变化。为避免把原 Key 静默发送到新地址，请重新填写 API Key，或明确勾选复用当前 Key。")
    for key in DEFAULT_SETTINGS:
        if key in values and isinstance(values[key], str) and values[key].strip():
            current[key] = values[key].strip()
    if values.get("clear_api_key"):
        current["api_key"] = ""
    elif isinstance(values.get("api_key"), str) and values["api_key"].strip():
        current["api_key"] = values["api_key"].strip()
    payload = {k: current[k] for k in DEFAULT_SETTINGS}
    payload["api_key_protected"] = protect(current["api_key"])
    with _lock:
        SETTINGS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return public_settings()


PREFERENCES_PATH = DATA / "workspace-preferences.json"
_PREFERENCE_DEFAULTS = {"sidebar_width": 280, "sidebar_collapsed": False}


def get_workspace_preferences() -> dict[str, Any]:
    path = DATA / "workspace-preferences.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        payload = {}
    result = dict(_PREFERENCE_DEFAULTS)
    if isinstance(payload, dict):
        width = payload.get("sidebar_width")
        if isinstance(width, (int, float)) and not isinstance(width, bool):
            result["sidebar_width"] = max(180, min(420, int(width)))
        if isinstance(payload.get("sidebar_collapsed"), bool):
            result["sidebar_collapsed"] = payload["sidebar_collapsed"]
    return result


def save_workspace_preferences(values: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("工作区偏好格式无效。")
    result = get_workspace_preferences()
    if "sidebar_width" in values:
        width = values["sidebar_width"]
        if isinstance(width, bool) or not isinstance(width, (int, float)):
            raise ValueError("侧栏宽度必须是数字。")
        result["sidebar_width"] = max(180, min(420, int(width)))
    if "sidebar_collapsed" in values:
        if not isinstance(values["sidebar_collapsed"], bool):
            raise ValueError("侧栏折叠状态必须是布尔值。")
        result["sidebar_collapsed"] = values["sidebar_collapsed"]
    destination = DATA / "workspace-preferences.json"
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with _lock:
        DATA.mkdir(parents=True, exist_ok=True)
        try:
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return result


def safe_filename(name: str) -> str:
    name = Path(name or "paper.pdf").name
    name = re.sub(r"[^\w.()\- 一-龥]+", "_", name).strip(" .")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:180] or "paper.pdf"


def _sha256(path: Path) -> str:
    try:
        stat = path.stat()
        key = (str(path.resolve()), int(stat.st_size), int(stat.st_mtime_ns), int(getattr(stat, "st_ctime_ns", 0)))
        cached = _sha_cache.get(key)
        if cached:
            return cached
    except OSError:
        key = None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    value = digest.hexdigest()
    if key is not None:
        _sha_cache[key] = value
        if len(_sha_cache) > 512:
            _sha_cache.pop(next(iter(_sha_cache)))
    return value


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _translation_meta_path(job: dict[str, Any]) -> Path:
    return JOBS / job["id"] / "translation-meta.json"


def _external_source_meta_path(job: dict[str, Any]) -> Path:
    # Kept outside job.json so a normal HTTP job response never contains the
    # user's original absolute path. Only native-import translation code reads
    # this private per-job record.
    return JOBS / job["id"] / EXTERNAL_SOURCE_META_NAME


def _write_external_source(job: dict[str, Any], source: Path) -> None:
    target = _external_source_meta_path(job)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps({"path": str(source)}, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _read_external_source(job: dict[str, Any]) -> Path | None:
    try:
        payload = json.loads(_external_source_meta_path(job).read_text(encoding="utf-8"))
        raw_path = payload.get("path")
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw_path, str) or not raw_path:
        return None
    try:
        source = Path(raw_path).resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None
    return source if source.is_file() and source.suffix.lower() == ".pdf" else None


def _read_translation_meta(job: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(_translation_meta_path(job).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _job_relative_file(job: dict[str, Any], relative: Any) -> Path | None:
    if not isinstance(relative, str) or not relative:
        return None
    job_dir = (JOBS / job["id"]).resolve()
    candidate = (job_dir / relative).resolve()
    try:
        candidate.relative_to(job_dir)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _translation_is_current(job: dict[str, Any]) -> bool:
    if not job.get("source_sha256"):
        return False
    source = JOBS / job["id"] / "source.pdf"
    if not source.is_file():
        return False
    meta = _read_translation_meta(job)
    if meta.get("source_sha256") != job.get("source_sha256") or meta.get("source_sha256") != _sha256(source):
        return False
    if meta.get("scope") not in {"full", "trial", "demo"}:
        return False
    translated = _job_relative_file(job, meta.get("translated_file"))
    bilingual = _job_relative_file(job, meta.get("bilingual_file"))
    if not translated or not bilingual:
        return False
    return meta.get("translated_sha256") == _sha256(translated) and meta.get("bilingual_sha256") == _sha256(bilingual)


@pdf_serialized
def _extract_translated_title(path: Path, fallback: str) -> str:
    """Read a geometrically credible Chinese title from the first page."""
    runtime = ROOT / (".runtime311" if sys.version_info[:2] == (3, 11) else ".runtime")
    if runtime.is_dir() and str(runtime) not in sys.path:
        sys.path.insert(0, str(runtime))
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        document = fitz.open(str(path))
        page = document[0]
        blocks = page.get_text("dict").get("blocks", [])
        spans = []
        for block in blocks:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = re.sub(r"\s+", " ", str(span.get("text") or "")).strip()
                    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
                    if not text or chinese < 2 or len(text) > 80:
                        continue
                    bbox = span.get("bbox") or (0, 0, 0, page.rect.height)
                    if float(bbox[1]) > float(page.rect.height) * 0.55:
                        continue
                    if re.search(r"(摘要|abstract|关键词|作者|单位|大学|医院|研究院|doi|https?://|\b20\d{2}\b)", text, re.I):
                        continue
                    spans.append({"text": text, "size": float(span.get("size") or 0), "x": float(bbox[0]), "y": float(bbox[1])})
        document.close()
        if not spans:
            return fallback
        largest = max(item["size"] for item in spans)
        title_spans = [item for item in spans if item["size"] >= max(11.0, largest * 0.78)]
        title_spans.sort(key=lambda item: (item["y"], item["x"]))
        candidate = "".join(item["text"] for item in title_spans[:3]).strip()
        if 2 <= len(re.findall(r"[\u3400-\u9fff]", candidate)) <= 80 and len(candidate) <= 120 and not re.search(r"[。！？；:：]$", candidate):
            return candidate
    except Exception:
        return fallback
    return fallback


def _fill_translation_title(job: dict[str, Any], translated: Path | None = None) -> bool:
    if job.get("translated_title") or job.get("title_checked"):
        return False
    translated = translated or _job_relative_file(job, job.get("translated_file"))
    if not translated:
        return False
    fallback = str(job.get("display_title") or job.get("filename") or "未命名论文")
    title = _extract_translated_title(translated, fallback)
    job["translated_title"] = title if title != fallback else None
    job["display_title"] = title or fallback
    job["title_checked"] = True
    return True


def _full_translation_paths(job: dict[str, Any]) -> tuple[Path, Path]:
    job_dir = JOBS / job["id"]
    stem = Path(job["filename"]).stem
    target_dir = job_dir / "translate"
    return target_dir / f"{stem}.zh.pdf", target_dir / f"{stem}.zh.bilingual.pdf"


def _redact_engine_line(line: str, api_key: str = "") -> str:
    if api_key:
        line = line.replace(api_key, "[REDACTED]")
    return re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", line)


def _apply_progress_event(job: dict[str, Any], event: dict[str, Any]) -> None:
    job["stage"] = event.get("stage", "engine")
    raw_progress = event.get("progress")
    if isinstance(raw_progress, (int, float)):
        # Reserve the final 5% for publishing and validation. This keeps a
        # late engine event at 99% from being followed by a visible 90% drop.
        mapped = round(min(95.0, max(0.0, float(raw_progress) * 0.95)), 2)
        previous = job.get("progress")
        job["progress"] = max(float(previous), mapped) if isinstance(previous, (int, float)) else mapped
        job["progress_indeterminate"] = False
    else:
        job["progress"] = None
        job["progress_indeterminate"] = True
    if "stage_current" in event:
        job["stage_current"] = event["stage_current"]
    if "stage_total" in event:
        job["stage_total"] = event["stage_total"]


def _write_translation_meta(job: dict[str, Any], scope: str, translated: Path, bilingual: Path) -> None:
    job_dir = JOBS / job["id"]
    payload = {
        "source_sha256": job["source_sha256"],
        "scope": scope,
        "translated_file": translated.relative_to(job_dir).as_posix(),
        "bilingual_file": bilingual.relative_to(job_dir).as_posix(),
        "translated_sha256": _sha256(translated),
        "bilingual_sha256": _sha256(bilingual),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = _translation_meta_path(job).with_name(f".translation-meta.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, _translation_meta_path(job))
    finally:
        temporary.unlink(missing_ok=True)


def _manifest_outputs_match(meta: dict[str, Any], job: dict[str, Any], scope: str, translated: Path, bilingual: Path) -> bool:
    """Allow replacement only for an intact output owned by this manifest."""
    job_dir = JOBS / job["id"]
    try:
        translated_rel = translated.relative_to(job_dir).as_posix()
        bilingual_rel = bilingual.relative_to(job_dir).as_posix()
    except ValueError:
        return False
    return (
        meta.get("translated_file") == translated_rel
        and meta.get("bilingual_file") == bilingual_rel
        and meta.get("source_sha256") == job.get("source_sha256")
        and meta.get("scope") == scope
        and translated.is_file()
        and bilingual.is_file()
        and meta.get("translated_sha256") == _sha256(translated)
        and meta.get("bilingual_sha256") == _sha256(bilingual)
    )


def _translation_output_paths(job: dict[str, Any], scope: str) -> tuple[Path, Path]:
    """Choose stable names while preserving unknown or edited PDFs."""
    job_dir = JOBS / job["id"]
    stem = Path(job["filename"]).stem
    target_dir = job_dir / "translate" if scope == "full" else job_dir / ("trial" if scope == "trial" else "demo")
    translated_name = f"{stem}.zh.pdf" if scope == "full" else f"{stem}.{scope}.zh.pdf"
    bilingual_name = f"{stem}.zh.bilingual.pdf" if scope == "full" else f"{stem}.{scope}.bilingual.pdf"
    canonical = (target_dir / translated_name, target_dir / bilingual_name)
    meta = _read_translation_meta(job)
    if not canonical[0].exists() and not canonical[1].exists():
        return canonical
    if _manifest_outputs_match(meta, job, scope, *canonical):
        return canonical
    suffix = (job.get("source_sha256") or _sha256(job_dir / "source.pdf"))[:12]
    for index in range(1000):
        tag = suffix if index == 0 else f"{suffix}-{index + 1}"
        candidate = (target_dir / f"{stem}.{tag}.zh.pdf", target_dir / f"{stem}.{tag}.zh.bilingual.pdf")
        if not candidate[0].exists() and not candidate[1].exists():
            return candidate
        if _manifest_outputs_match(meta, job, scope, *candidate):
            return candidate
    raise RuntimeError("译文输出目录中已有过多同名文件，无法安全生成新译文。")


def _publish_translation_outputs(job: dict[str, Any], generated_translated: Path, generated_bilingual: Path, scope: str | None = None) -> tuple[Path, Path]:
    job_dir = JOBS / job["id"]
    scope = scope or ("full" if job.get("mode") == "full" else "trial")
    translated, bilingual = _translation_output_paths(job, scope)
    _atomic_copy(generated_translated, translated)
    _atomic_copy(generated_bilingual if generated_bilingual.is_file() else generated_translated, bilingual)
    _write_translation_meta(job, scope, translated, bilingual)
    return translated, bilingual


def _find_cached_full_translation(source_sha256: str, exclude_job_id: str | None = None) -> tuple[Path, Path] | None:
    if not JOBS.is_dir():
        return None
    for meta_path in JOBS.glob("*/translation-meta.json"):
        job_id = meta_path.parent.name
        if exclude_job_id and job_id == exclude_job_id:
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict) or meta.get("scope") != "full" or meta.get("source_sha256") != source_sha256:
            continue
        job = {"id": job_id}
        translated = _job_relative_file(job, meta.get("translated_file"))
        bilingual = _job_relative_file(job, meta.get("bilingual_file")) or translated
        if translated and meta.get("translated_sha256") == _sha256(translated) and bilingual and meta.get("bilingual_sha256") == _sha256(bilingual):
            return translated, bilingual or translated
    return None


def _reuse_full_translation(job: dict[str, Any]) -> bool:
    source = JOBS / job["id"] / "source.pdf"
    if not source.is_file():
        return False
    source_sha256 = _sha256(source)
    job["source_sha256"] = source_sha256
    meta = _read_translation_meta(job)
    existing_translated = _job_relative_file(job, meta.get("translated_file")) if meta.get("source_sha256") == source_sha256 and meta.get("scope") == "full" else None
    existing_bilingual = _job_relative_file(job, meta.get("bilingual_file")) if existing_translated else None
    if existing_translated and (not existing_bilingual or meta.get("translated_sha256") != _sha256(existing_translated) or meta.get("bilingual_sha256") != _sha256(existing_bilingual)):
        existing_translated = None
        existing_bilingual = None
    cached = (existing_translated, existing_bilingual or existing_translated) if existing_translated else _find_cached_full_translation(source_sha256, job["id"])
    if not cached:
        return False
    translated, bilingual = _translation_output_paths(job, "full")
    _atomic_copy(cached[0], translated)
    _atomic_copy(cached[1], bilingual)
    _write_translation_meta(job, "full", translated, bilingual)
    job.update({"translated_file": translated.relative_to(JOBS / job["id"]).as_posix(), "bilingual_file": bilingual.relative_to(JOBS / job["id"]).as_posix(), "translation_scope": "full", "translation_reused": True, "message": "已复用同源全文译文，未再次调用 API。"})
    return True


def _external_member_path(directory: Path, relative: Any) -> Path | None:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return None
    candidate = (directory / relative).resolve()
    try:
        candidate.relative_to(directory.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _external_manifest_path(directory: Path, stem: str, suffix: str = "") -> Path:
    return directory / f"{stem}{suffix}.zh.manifest.json"


def _read_external_manifest(source: Path, source_sha256: str) -> tuple[Path, Path] | None:
    directory = source.parent / "translate"
    if not directory.is_dir():
        return None
    stem = source.stem
    manifests = [_external_manifest_path(directory, stem)]
    manifests.extend(sorted(directory.glob(f"{stem}.*.zh.manifest.json")))
    seen: set[Path] = set()
    for manifest_path in manifests:
        manifest_path = manifest_path.resolve()
        if manifest_path in seen:
            continue
        seen.add(manifest_path)
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("scope") != "full" or payload.get("source_sha256") != source_sha256:
            continue
        translated = _external_member_path(directory, payload.get("translated_file"))
        bilingual = _external_member_path(directory, payload.get("bilingual_file"))
        if (
            translated
            and bilingual
            and payload.get("translated_sha256") == _sha256(translated)
            and payload.get("bilingual_sha256") == _sha256(bilingual)
        ):
            return translated, bilingual
    return None


def _external_manifest_matches(manifest_path: Path, source: Path, source_sha256: str, translated: Path, bilingual: Path) -> bool:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    directory = source.parent / "translate"
    return (
        isinstance(payload, dict)
        and payload.get("scope") == "full"
        and payload.get("source_sha256") == source_sha256
        and payload.get("translated_file") == translated.name
        and payload.get("bilingual_file") == bilingual.name
        and payload.get("translated_sha256") == _sha256(translated)
        and payload.get("bilingual_sha256") == _sha256(bilingual)
        and manifest_path.parent.resolve() == directory.resolve()
    )


def _external_output_paths(source: Path, source_sha256: str) -> tuple[Path, Path, Path]:
    directory = source.parent / "translate"
    stem = source.stem
    canonical = (directory / f"{stem}.zh.pdf", directory / f"{stem}.zh.bilingual.pdf", _external_manifest_path(directory, stem))
    if not any(path.exists() for path in canonical):
        return canonical
    if all(path.is_file() for path in canonical) and _external_manifest_matches(canonical[2], source, source_sha256, canonical[0], canonical[1]):
        return canonical
    suffix = source_sha256[:12]
    for index in range(1000):
        tag = suffix if index == 0 else f"{suffix}-{index + 1}"
        candidate = (
            directory / f"{stem}.{tag}.zh.pdf",
            directory / f"{stem}.{tag}.zh.bilingual.pdf",
            _external_manifest_path(directory, stem, f".{tag}"),
        )
        if not any(path.exists() for path in candidate):
            return candidate
        if all(path.is_file() for path in candidate) and _external_manifest_matches(candidate[2], source, source_sha256, candidate[0], candidate[1]):
            return candidate
    raise RuntimeError("原文目录中已有过多同名译文，无法安全写入旁边目录。")


def _write_external_manifest(path: Path, source: Path, source_sha256: str, translated: Path, bilingual: Path) -> None:
    payload = {
        "schema": 1,
        "scope": "full",
        "source_name": source.name,
        "source_sha256": source_sha256,
        "translated_file": translated.name,
        "bilingual_file": bilingual.name,
        "translated_sha256": _sha256(translated),
        "bilingual_sha256": _sha256(bilingual),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_external_full_outputs(job: dict[str, Any], translated: Path, bilingual: Path) -> str | None:
    source = _read_external_source(job)
    if not source:
        return None
    expected_sha256 = job.get("source_sha256")
    try:
        if not expected_sha256 or _sha256(source) != expected_sha256:
            return "原始文件在翻译期间已变化，未覆盖其旁边目录；job 内译文已保留。"
        target_translated, target_bilingual, manifest = _external_output_paths(source, expected_sha256)
        target_translated.parent.mkdir(parents=True, exist_ok=True)
        _atomic_copy(translated, target_translated)
        _atomic_copy(bilingual, target_bilingual)
        _write_external_manifest(manifest, source, expected_sha256, target_translated, target_bilingual)
        return None
    except (OSError, RuntimeError) as exc:
        return f"原文目录不可写，未保存旁边译文；job 内译文已保留（{exc}）。"


def _reuse_external_translation(job: dict[str, Any], source: Path) -> bool:
    source_sha256 = _sha256(source)
    cached = _read_external_manifest(source, source_sha256)
    if not cached:
        return False
    translated, bilingual = _translation_output_paths(job, "full")
    _atomic_copy(cached[0], translated)
    _atomic_copy(cached[1], bilingual)
    job["source_sha256"] = source_sha256
    _write_translation_meta(job, "full", translated, bilingual)
    job.update({
        "status": "completed",
        "progress": 100,
        "progress_indeterminate": False,
        "stage": "reused",
        "translated_file": translated.relative_to(JOBS / job["id"]).as_posix(),
        "bilingual_file": bilingual.relative_to(JOBS / job["id"]).as_posix(),
        "translation_scope": "full",
        "translation_reused": True,
        "error": "",
        "message": "已复用原文目录旁边的同源全文译文，未再次调用 API。",
    })
    return True


def parse_pages(value: str, page_count: int) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    for part in value.split(","):
        if not re.fullmatch(r"\d+(?:-\d*)?", part.strip()):
            raise ValueError("页码范围格式应为 1,2,4-6 或 3-；页码从 1 开始。")
        bits = part.strip().split("-")
        start = int(bits[0])
        end = int(bits[1]) if len(bits) > 1 and bits[1] else page_count
        if start < 1 or end < start or start > page_count or end > page_count:
            raise ValueError(f"页码超出范围：本文件共有 {page_count} 页。")
    return value


def load_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        if job_id in _deleted_jobs:
            return None
        if job_id in _jobs:
            result = dict(_jobs[job_id])
            if result.get("status") == "completed" and _fill_translation_title(result):
                save_job(result)
            return result
        path = JOBS / job_id / "job.json"
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(result, dict) and result.get("status") == "completed" and _fill_translation_title(result):
                save_job(result)
            return result
        except (FileNotFoundError, json.JSONDecodeError):
            return None


def _save_job_unlocked(job: dict[str, Any]) -> None:
    job_dir = JOBS / job["id"]
    if not isinstance(job.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", job["id"]):
        raise ValueError("job_id 无效。")
    candidate = Path(os.path.abspath(JOBS / job["id"]))
    try:
        candidate.relative_to(Path(os.path.abspath(JOBS)))
    except ValueError as exc:
        raise ValueError("文献任务路径不安全。") from exc
    if job["id"] in _deleted_jobs:
        raise FileNotFoundError("文献任务已删除，拒绝写回旧任务。")
    job_dir.mkdir(parents=True, exist_ok=True)
    destination = job_dir / "job.json"
    temporary = job_dir / f".job.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            catalog.update(DATA, job)
        except Exception:
            # The job JSON is the compatibility source of truth; a transient
            # index failure must not make an already completed translation fail.
            pass
        # Publish the JSON only after catalog.update has closed its SQLite
        # connection. Pollers therefore cannot observe completed while the
        # database is still held open on Windows.
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    with _lock:
        _jobs[job["id"]] = dict(job)


def save_job(job: dict[str, Any]) -> None:
    with _lock:
        _save_job_unlocked(job)


def list_jobs() -> list[dict[str, Any]]:
    ensure_dirs()
    result = []
    for row in catalog.list_documents(DATA):
        if row.get("status") == "completed" and not row.get("translated_title"):
            # Only legacy rows lacking a title are reopened once; normal list
            # polling is served entirely from SQLite.
            job = load_job(str(row.get("job_id")))
            if job:
                row["display_title"] = job.get("display_title") or row.get("display_title")
                row["translated_title"] = job.get("translated_title")
                _enqueue_markdown_export(job)
        elif row.get("status") == "completed":
            _enqueue_markdown_export({
                "id": row.get("job_id"), "status": "completed", "filename": row.get("filename"),
                "translated_file": row.get("translated_pdf"), "source_sha256": row.get("source_sha256"),
                "translated_sha256": row.get("translated_sha256"), "translation_scope": row.get("translation_scope"),
                "mode": row.get("mode"), "pages": row.get("pages"), "page_count": row.get("page_count"),
                "demo_mode": bool(row.get("demo_mode")),
            })
        result.append({
            "id": row.get("job_id"), "filename": row.get("filename"),
            "display_title": row.get("display_title"), "translated_title": row.get("translated_title"),
            "created_at": row.get("created_at"), "status": row.get("status"),
            "mode": row.get("mode"), "page_count": row.get("page_count"),
            "error": row.get("error"), "demo_mode": bool(row.get("demo_mode")),
        })
    return sorted(result, key=lambda x: x.get("created_at", ""), reverse=True)


def _find_babeldoc() -> list[str] | None:
    local_launcher = ROOT / "babeldoc_local.py"
    engine_python = ROOT / ".runtime311" / "babeldoc" / "main.py"
    if engine_python.is_file() and local_launcher.is_file():
        configured_python = os.environ.get("PAPER_TRANSLATOR_PYTHON311", "").strip()
        if configured_python and Path(configured_python).is_file():
            return [configured_python, str(local_launcher)]
        conda_python = Path(r"E:\miniconda\envs\EXO\python.exe")
        if conda_python.is_file():
            return [str(conda_python), str(local_launcher)]
        py_launcher = shutil.which("py")
        if py_launcher:
            try:
                probe = subprocess.run([py_launcher, "-3.11", "-c", "import sys; raise SystemExit(sys.version_info[:2] != (3, 11))"], capture_output=True, timeout=10, **_hidden_process_kwargs())
                if probe.returncode == 0:
                    return [py_launcher, "-3.11", str(local_launcher)]
            except (OSError, subprocess.SubprocessError):
                pass
    if (ROOT / ".runtime" / "babeldoc" / "main.py").is_file() and local_launcher.is_file():
        return [os.sys.executable, str(local_launcher)]
    executable = shutil.which("babeldoc")
    if executable:
        return [executable]
    # uv tool installations may not be on PATH; this command is a useful
    # explicit error if available without importing BabelDOC into this app.
    if shutil.which("uv"):
        return ["uv", "tool", "run", "babeldoc"]
    return None


def _hidden_process_kwargs() -> dict[str, Any]:
    """Hide bundled Python consoles on Windows while remaining portable."""
    creation_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": creation_flag} if creation_flag else {}


def _engine_output_pdfs(output_dir: Path, started_at: float) -> list[Path]:
    """Return fresh normal outputs and ignore debug/inspection PDFs."""
    debug_tokens = ("debug", "charbox", "char_box", "show-char-box")
    candidates = []
    for path in output_dir.rglob("*.pdf"):
        lowered = path.name.lower()
        if any(token in lowered for token in debug_tokens):
            continue
        try:
            if path.stat().st_mtime + 1.0 < started_at:
                continue
        except OSError:
            continue
        candidates.append(path)
    return candidates


def _run_babeldoc(job: dict[str, Any], settings: dict[str, Any]) -> tuple[Path, Path]:
    command = _find_babeldoc()
    if not command:
        raise RuntimeError("未找到 BabelDOC。请按 README 安装 BabelDOC，或在设置中确认本地环境已提供 babeldoc 命令。无 API Key 时可切换为演示模式验证流程。")
    job_dir = JOBS / job["id"]
    output_dir = job_dir / "babeldoc-output"
    output_dir.mkdir(exist_ok=True)
    run_started = time.time()
    (job_dir / "engine.log").write_text("", encoding="utf-8")
    source = job_dir / "source.pdf"
    # BabelDOC documents both CLI and TOML configuration. Keep the secret out
    # of the process command line; the short-lived config is deleted in the
    # finally block before the job returns.
    config_path: Path | None = None
    config_text = "\n".join([
        "[babeldoc]",
        "openai = true",
        f"openai-model = {json.dumps(settings['model'])}",
        f"openai-base-url = {json.dumps(settings['base_url'])}",
        f"openai-api-key = {json.dumps(settings['api_key'])}",
        f"lang-in = {json.dumps(settings['language_in'])}",
        f"lang-out = {json.dumps(settings['language_out'])}",
        f"output = {json.dumps(str(output_dir))}",
        f"working-dir = {json.dumps(str(job_dir / 'babeldoc-work'))}",
        'watermark-output-mode = "no_watermark"',
        "report-interval = 1",
    ])
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".toml", prefix="paper-translator-", delete=False) as config_file:
            config_file.write(config_text)
            config_path = Path(config_file.name)
        args = command + ["--files", str(source), "--config", str(config_path)]
        if urlparse(settings["base_url"]).hostname == "api.deepseek.com":
            # DeepSeek V4 defaults to thinking mode. Its reasoning can consume
            # the response budget before BabelDOC receives usable text/JSON.
            args += ["--openai-thinking", "disabled", "--no-send-temperature"]
        if job.get("pages"):
            args += ["--pages", job["pages"]]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, env=env, **_hidden_process_kwargs())
        lines: queue.Queue[str | None] = queue.Queue()

        def read_engine_output() -> None:
            assert process.stdout is not None
            for output_line in process.stdout:
                lines.put(output_line)
            lines.put(None)

        reader = threading.Thread(target=read_engine_output, daemon=True)
        reader.start()
        engine_log = job_dir / "engine.log"
        last_save = 0.0
        eof = False
        while not eof or process.poll() is None:
            try:
                output_line = lines.get(timeout=0.25)
            except queue.Empty:
                output_line = ""
            if output_line is None:
                eof = True
                continue
            if not output_line:
                continue
            with engine_log.open("a", encoding="utf-8") as log_stream:
                log_stream.write(_redact_engine_line(output_line, settings.get("api_key", "")))
            event = parse_engine_progress(output_line)
            if event:
                _apply_progress_event(job, event)
                now = time.monotonic()
                if now - last_save >= 0.25:
                    save_job(job)
                    last_save = now
        process.wait(timeout=30)
        reader.join(timeout=2)
        completed = process
    finally:
        if config_path:
            config_path.unlink(missing_ok=True)
    diagnostic = (job_dir / "engine.log").read_text(encoding="utf-8") if (job_dir / "engine.log").exists() else ""
    details = diagnostic.strip()[-1200:] or "BabelDOC 未返回错误详情"
    if completed.returncode != 0:
        raise RuntimeError(f"BabelDOC 翻译失败：{details}")
    pdfs = _engine_output_pdfs(output_dir, run_started)
    if not pdfs:
        raise RuntimeError(f"BabelDOC 未生成 PDF：{details}")
    bilingual = next((p for p in pdfs if any(s in p.name.lower() for s in ("dual", "bilingual", "双语"))), None)
    translated = next((p for p in pdfs if any(s in p.name.lower() for s in ("mono", "translate", "translated", "译"))), None)
    bilingual = bilingual or pdfs[0]
    translated = translated or pdfs[-1]
    published_translated, published_bilingual = _publish_translation_outputs(job, translated, bilingual)
    _validate_translation_output(source, published_translated, job)
    return published_translated, published_bilingual


@pdf_serialized
def _validate_translation_output(source: Path, translated: Path, job: dict[str, Any]) -> None:
    """Reject a PDF that silently lost text-heavy pages during translation."""
    runtime = ROOT / (".runtime311" if os.sys.version_info[:2] == (3, 11) else ".runtime")
    if runtime.is_dir() and str(runtime) not in os.sys.path:
        os.sys.path.insert(0, str(runtime))
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    original = fitz.open(str(source))
    output = fitz.open(str(translated))
    try:
        if job.get("mode") == "full":
            requested = list(range(len(original)))
        else:
            requested = []
            for part in str(job.get("pages", "")).split(","):
                if not part:
                    continue
                bounds = part.split("-", 1)
                first = int(bounds[0])
                last = int(bounds[1]) if len(bounds) == 2 and bounds[1] else len(original)
                requested.extend(range(first - 1, last))
            requested = sorted(set(requested))
        if len(output) == len(original):
            page_pairs = [(index, index) for index in requested]
        elif len(output) == len(requested):
            page_pairs = [(source_index, output_index) for output_index, source_index in enumerate(requested)]
        else:
            raise RuntimeError("译文 PDF 页数与请求的页码不匹配，翻译结果未通过完整性检查。")
        for source_index, output_index in page_pairs:
            source_count = len(original[source_index].get_text().strip())
            translated_count = len(output[output_index].get_text().strip())
            if source_count >= 200 and translated_count < max(60, source_count * 0.12):
                raise RuntimeError(f"译文第 {source_index + 1} 页正文疑似缺失（原文 {source_count} 字符，译文 {translated_count} 字符）。请检查模型输出或重试；原文仍可阅读。")
    finally:
        original.close()
        output.close()


def _parse_engine_usage(log_text: str) -> dict[str, Any] | None:
    """Read provider-reported usage from BabelDOC's final summary."""
    flat = re.sub(r"\s+", " ", re.sub(r"\bmain\.py:\d+\b", " ", log_text))

    def last(pattern: str) -> int | None:
        matches = re.findall(pattern, flat, flags=re.IGNORECASE)
        return int(matches[-1]) if matches else None

    total = last(r"INFO:babeldoc\.main:Total tokens:\s*(\d+)")
    prompt = last(r"INFO:babeldoc\.main:Prompt tokens:\s*(\d+)")
    completion = last(r"INFO:babeldoc\.main:Completion tokens:\s*(\d+)")
    if total is None or prompt is None or completion is None:
        return None
    cache_hit = last(r"INFO:babeldoc\.main:Cache hit prompt tokens:\s*(\d+)")
    term_match = re.findall(
        r"Term extraction tokens:\s*total=(\d+)\s+prompt=(\d+)\s+completion=(\d+)\s+cache_hit_prompt=(\d+)",
        flat,
        flags=re.IGNORECASE,
    )
    separate_match = re.findall(
        r"Term extraction translator raw tokens:\s*total=(\d+)\s+prompt=(\d+)\s+completion=(\d+)\s+cache_hit_prompt=(\d+)",
        flat,
        flags=re.IGNORECASE,
    )
    separate = tuple(map(int, separate_match[-1])) if separate_match else None
    term = tuple(map(int, term_match[-1])) if term_match else None
    return {
        "total_tokens": total + (separate[0] if separate else 0),
        "prompt_tokens": prompt + (separate[1] if separate else 0),
        "completion_tokens": completion + (separate[2] if separate else 0),
        "cache_hit_prompt_tokens": (cache_hit or 0) + (separate[3] if separate else 0),
        "term_extraction_tokens": term[0] if term else None,
        "source": "provider_usage",
    }


def _record_engine_usage(job: dict[str, Any]) -> None:
    log_path = JOBS / job["id"] / "engine.log"
    try:
        usage = _parse_engine_usage(log_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    if usage is None:
        return
    runs = list(job.get("usage_runs") or [])
    runs.append({"at": datetime.now(timezone.utc).isoformat(), "mode": job.get("mode"), "pages": job.get("pages"), "status": job.get("status"), **usage})
    job["usage_runs"] = runs
    fields = ("total_tokens", "prompt_tokens", "completion_tokens", "cache_hit_prompt_tokens")
    job["token_usage"] = {field: sum(int(run.get(field, 0)) for run in runs) for field in fields}
    job["token_usage"]["last_run"] = usage
    job["token_usage"]["run_count"] = len(runs)


def _run_demo(job: dict[str, Any]) -> tuple[Path, Path]:
    # Preserve the source PDF as a clearly labelled workflow-only placeholder.
    # It is never presented as a translation, and keeps local UI/preview tests
    # usable without a real API key or a network call.
    source = JOBS / job["id"] / "source.pdf"
    translated = JOBS / job["id"] / "demo-generated-translated.pdf"
    bilingual = JOBS / job["id"] / "demo-generated-bilingual.pdf"
    shutil.copyfile(source, translated)
    shutil.copyfile(source, bilingual)
    return _publish_translation_outputs(job, translated, bilingual, scope="demo")


def _worker(job: dict[str, Any]) -> None:
    engine_started = False
    job_id = str(job.get("id", "")) or None
    diagnostic("translation_started", job_id=job_id, fields={"phase": "worker"})
    try:
        job["status"] = "running"
        job["progress"] = None
        job["progress_indeterminate"] = True
        job["stage"] = "preparing"
        job["stage_current"] = 0
        job["stage_total"] = None
        save_job(job)
        source = JOBS / job["id"] / "source.pdf"
        if source.is_file():
            # Recompute on every run, including trial/demo runs, so a source
            # edited in place cannot retain the previous manifest hash.
            job["source_sha256"] = _sha256(source)
            save_job(job)
        if job.get("mode") == "full" and not job.get("demo_mode") and _reuse_full_translation(job):
            translated = JOBS / job["id"] / job["translated_file"]
            bilingual = JOBS / job["id"] / job["bilingual_file"]
            _fill_translation_title(job, translated)
            warning = _publish_external_full_outputs(job, translated, bilingual)
            if warning:
                job["message"] = f"{job.get('message', '翻译完成。')} {warning}"
            job.update({"status": "completed", "progress": 100, "progress_indeterminate": False, "stage": "reused", "message": "已复用同源全文译文，未再次调用 API。", "error": ""})
            if warning:
                job["message"] = f"已复用同源全文译文，未再次调用 API。{warning}"
            save_job(job)
            _enqueue_markdown_export(job)
            diagnostic("translation_finished", job_id=job_id, fields={"status": "reused", "phase": "worker"})
            return
        settings = read_settings()
        if job["demo_mode"]:
            diagnostic("translation_phase", job_id=job_id, fields={"phase": "demo"})
            translated, bilingual = _run_demo(job)
            job.update({"progress": max(float(job.get("progress") or 0), 95.0), "progress_indeterminate": False, "stage": "publishing"})
        else:
            if not settings["api_key"]:
                raise RuntimeError("尚未设置 API Key。请先在设置页保存密钥，或勾选“无 Key 演示模式”验证上传与阅读流程。")
            job["stage"] = "engine"
            save_job(job)
            engine_started = True
            diagnostic("translation_phase", job_id=job_id, fields={"phase": "engine"})
            translated, bilingual = _run_babeldoc(job, settings)
        external_warning = _publish_external_full_outputs(job, translated, bilingual) if job.get("mode") == "full" and not job.get("demo_mode") else None
        job["translated_file"] = translated.relative_to(JOBS / job["id"]).as_posix()
        job["bilingual_file"] = bilingual.relative_to(JOBS / job["id"]).as_posix()
        _fill_translation_title(job, translated)
        job["translation_scope"] = "demo" if job.get("demo_mode") else ("full" if job.get("mode") == "full" else "trial")
        job["translation_reused"] = False
        job["status"] = "completed"
        job["progress"] = 100
        job["progress_indeterminate"] = False
        job["stage"] = "completed"
        job["message"] = "演示模式：PDF 已保留用于阅读流程验证，未执行真实翻译；这是试译流程占位结果。" if job["demo_mode"] else ("试译完成：该译文仅覆盖所选页码，不会作为全文译文自动加载。" if job.get("mode") == "trial" else "翻译完成。")
        if external_warning:
            job["message"] += f" {external_warning}"
    except Exception as exc:
        job["status"] = "failed"
        job["progress"] = None
        job["progress_indeterminate"] = True
        job["stage"] = "failed"
        job["error"] = str(exc)
        diagnostic_exception("translation_failed", exc, job_id=job_id, fields={"phase": "worker"})
    if engine_started:
        _record_engine_usage(job)
    save_job(job)
    if job.get("status") == "completed":
        diagnostic("translation_finished", job_id=job_id, fields={"status": "completed", "phase": "worker"})
    _enqueue_markdown_export(job)


def _start_job(job: dict[str, Any]) -> None:
    job_id = str(job.get("id", ""))
    with _lock:
        _active_jobs.add(job_id)
    def run() -> None:
        try:
            _worker(job)
        finally:
            with _lock:
                _active_jobs.discard(job_id)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()


def _enqueue_markdown_export(job: dict[str, Any]) -> None:
    """Queue Markdown generation after the PDF is durably published."""
    if job.get("status") != "completed":
        return
    if _markdown_export_current(job):
        return
    data_root, jobs_root, job_id = DATA, JOBS, str(job.get("id", ""))
    key = f"{jobs_root.resolve()}::{job_id}"
    with _lock:
        if key in _markdown_scheduled:
            return
        _markdown_scheduled.add(key)

    def dispatch() -> None:
        try:
            from .markdown_export import enqueue_export
            current_path = jobs_root / job_id / "job.json"
            try:
                current = json.loads(current_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # A deleted job must never be resurrected by this stale timer.
                return
            enqueue_export(data_root, jobs_root, current)
        except Exception:
            # Markdown is an auxiliary export; PDF completion remains successful.
            pass
        finally:
            with _lock:
                _markdown_scheduled.discard(key)

    # Let callers finish their response and tests release temporary folders;
    # the export remains asynchronous and retries on the next completed/listed job.
    timer = threading.Timer(2.0, dispatch)
    timer.daemon = True
    timer.start()


def _markdown_export_current(job: dict[str, Any]) -> bool:
    """Compare cheap manifest/catalog fields; never hash PDFs while listing."""
    metadata_path = JOBS / str(job.get("id", "")) / "markdown" / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(metadata, dict):
        return False
    try:
        from .markdown_export import CONVERTER_NAME, CONVERTER_VERSION, EXPORT_VERSION, SCHEMA_VERSION
        if any(metadata.get(key) != value for key, value in {
            "version": EXPORT_VERSION, "schema_version": SCHEMA_VERSION,
            "converter": CONVERTER_NAME, "converter_version": CONVERTER_VERSION,
        }.items()):
            return False
    except Exception:
        return False
    scope = "demo" if job.get("demo_mode") else str(job.get("translation_scope") or job.get("mode") or "full").lower()
    if scope not in {"full", "trial", "demo"}:
        scope = "full"
    selected: list[int]
    try:
        selected = list(range(1, int(job.get("page_count") or 0) + 1)) if scope == "full" or not str(job.get("pages") or "").strip() else []
        if not selected:
            for part in str(job.get("pages") or "").split(","):
                bits = part.strip().split("-", 1)
                first = int(bits[0]); last = int(bits[1]) if len(bits) == 2 and bits[1] else int(job.get("page_count") or first)
                selected.extend(range(first, last + 1))
            selected = sorted(set(selected))
    except (TypeError, ValueError):
        return False
    try:
        translation_meta = _read_translation_meta(job)
    except Exception:
        translation_meta = {}
    return (
        metadata.get("source_sha256") == job.get("source_sha256")
        and metadata.get("translated_sha256") == translation_meta.get("translated_sha256")
        and metadata.get("scope") == scope
        and metadata.get("selected_pages") == selected
        and all((metadata_path.parent / name).is_file() for name in ("source.md", "translated.md"))
    )


def _is_reparse_point(path: Path) -> bool:
    """Return whether a path is a symlink or Windows reparse point."""
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _assert_owned_job_tree(job_dir: Path) -> None:
    jobs_root = Path(os.path.abspath(JOBS))
    if _is_reparse_point(JOBS) or _is_reparse_point(job_dir):
        raise ValueError("文献任务目录包含符号链接或联接点，已拒绝删除。")
    try:
        resolved = Path(os.path.abspath(job_dir))
        resolved.relative_to(jobs_root)
    except (OSError, ValueError) as exc:
        raise ValueError("文献任务路径不安全，已拒绝删除。") from exc
    try:
        descendants = list(job_dir.rglob("*"))
    except OSError as exc:
        raise ValueError("无法验证文献任务目录，已拒绝删除。") from exc
    for path in descendants:
        if _is_reparse_point(path):
            raise ValueError("文献任务目录包含符号链接或联接点，已拒绝删除。")
        try:
            Path(os.path.abspath(path)).relative_to(jobs_root)
        except (OSError, ValueError) as exc:
            raise ValueError("文献任务路径不安全，已拒绝删除。") from exc


def _markdown_job_key(job_id: str) -> str:
    return f"{JOBS.resolve()}::{job_id}"


def _delete_busy(job_id: str, job: dict[str, Any]) -> bool:
    if str(job.get("status", "")) in {"queued", "running"} or job_id in _active_jobs:
        return True
    if _markdown_job_key(job_id) in _markdown_scheduled:
        return True
    try:
        from . import markdown_export
        with markdown_export._pending_lock:
            return (str(JOBS.resolve()), job_id) in markdown_export._pending
    except Exception:
        # If queue state cannot be inspected, refuse deletion rather than
        # risking a concurrent Markdown writer.
        return True


def delete_job(job_id: str) -> dict[str, Any]:
    """Delete one software-owned job bundle and leave external files alone."""
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", job_id):
        raise ValueError("job_id 无效。")
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            try:
                payload = json.loads((JOBS / job_id / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = None
            job = payload if isinstance(payload, dict) else None
        if not isinstance(job, dict) or str(job.get("id", "")) != job_id:
            raise ValueError("文献任务不存在。")
        if _delete_busy(job_id, job):
            raise ValueError("文献正在处理中或 Markdown 导出尚未完成，请稍后再删除。")
        job_dir = JOBS / job_id
        _assert_owned_job_tree(job_dir)
        staging_root = DATA / ".delete-staging"
        if _is_reparse_point(staging_root):
            raise ValueError("删除暂存目录包含符号链接或联接点，已拒绝删除。")
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = staging_root / f"{job_id}.{uuid.uuid4().hex}"
        from . import markdown_export
        with markdown_export._index_lock:
            index_snapshots: dict[Path, bytes | None] = {}
            for index_path in (DATA / "index.json", DATA / "README.md"):
                try:
                    index_snapshots[index_path] = index_path.read_bytes()
                except FileNotFoundError:
                    index_snapshots[index_path] = None
                except OSError as exc:
                    raise OSError(f"无法读取 Markdown 索引，删除已取消：{exc}") from exc
            try:
                os.replace(job_dir, staging)
            except OSError as exc:
                raise OSError(f"文献库副本删除失败：{exc}") from exc
            try:
                catalog.remove(DATA, job_id)
            except Exception as exc:
                try:
                    os.replace(staging, job_dir)
                    catalog.update(DATA, job)
                except Exception as restore_exc:
                    raise OSError(f"文献库索引清理失败，且无法恢复文献库副本：{restore_exc}") from exc
                raise OSError(f"文献库索引清理失败，删除已取消：{exc}") from exc
            try:
                markdown_export.refresh_library_index(DATA, JOBS)
            except Exception as exc:
                try:
                    os.replace(staging, job_dir)
                    catalog.update(DATA, job)
                    for index_path, content in index_snapshots.items():
                        if content is None:
                            index_path.unlink(missing_ok=True)
                        else:
                            index_path.parent.mkdir(parents=True, exist_ok=True)
                            index_path.write_bytes(content)
                except Exception as restore_exc:
                    raise OSError(f"Markdown 索引刷新失败，且无法恢复删除前状态：{restore_exc}") from exc
                raise OSError(f"Markdown 索引刷新失败，删除已取消：{exc}") from exc
        _jobs.pop(job_id, None)
        try:
            shutil.rmtree(staging)
        except OSError as exc:
            # The hidden staging copy is outside JOBS and is safe to clean up
            # later; a successful index removal is still a completed delete.
            print(f"[paper2zh] 删除残余暂存清理失败：{exc}")
        _deleted_jobs.add(job_id)
        return job


def create_job(filename: str, content: bytes, pages: str, mode: str, demo_mode: bool, start_translation: bool = True, *, _track_lifecycle: bool = False) -> dict[str, Any]:
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("文件超过 100 MB 限制，请压缩后再试。")
    if not content.startswith(b"%PDF"):
        raise ValueError("上传内容不是有效 PDF 文件。")
    try:
        reader = PdfReader(io.BytesIO(content))
        page_count = len(reader.pages)
        pages = parse_pages(pages, page_count)
    except Exception:
        raise ValueError("无法读取 PDF 页数，请确认文件未损坏且未加密。")
    ensure_dirs()
    source_sha256 = hashlib.sha256(content).hexdigest()
    job_id = uuid.uuid4().hex
    filename_value = safe_filename(filename)
    existing_id = catalog.claim(DATA, source_sha256, job_id, filename=filename_value, created_at=datetime.now(timezone.utc).isoformat())
    if existing_id:
        existing = load_job(existing_id)
        for _ in range(100):
            if existing:
                break
            # Another importer may have committed the SQLite reservation just
            # before writing its compatibility JSON job file.
            time.sleep(0.01)
            existing = load_job(existing_id)
        if existing:
            duplicate = dict(existing)
            duplicate["duplicate"] = True
            duplicate["message"] = "已存在相同内容的文献，已返回现有文献，未重复导入或翻译。"
            return duplicate
        raise ValueError("相同内容的文献索引指向尚未完成的导入，请稍后重试。")
    job_dir = JOBS / job_id
    with _lock:
        _active_jobs.add(job_id)
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        with _lock:
            _active_jobs.discard(job_id)
        catalog.remove(DATA, job_id)
        raise
    source = job_dir / "source.pdf"
    try:
        source.write_bytes(content)
    except OSError:
        with _lock:
            _active_jobs.discard(job_id)
        shutil.rmtree(job_dir, ignore_errors=True)
        catalog.remove(DATA, job_id)
        raise
    actual_mode = mode if mode in {"trial", "full"} else "full"
    job = {"id": job_id, "filename": filename_value, "display_title": filename_value, "created_at": datetime.now(timezone.utc).isoformat(), "status": "queued" if start_translation else "imported", "progress": None, "progress_indeterminate": True, "stage": "queued" if start_translation else "imported", "stage_current": 0, "stage_total": None, "mode": actual_mode, "pages": pages, "page_count": page_count, "demo_mode": bool(demo_mode), "source_sha256": source_sha256, "translation_scope": None, "translation_reused": False, "translated_title": None, "title_checked": False, "error": "", "message": "已导入源 PDF，可先预览或开始翻译。" if not start_translation else "", "markdown": {"source": "markdown/source.md", "translated": "markdown/translated.md", "metadata": "markdown/metadata.json"}}
    try:
        save_job(job)
    except Exception:
        with _lock:
            _active_jobs.discard(job_id)
        shutil.rmtree(job_dir, ignore_errors=True)
        catalog.remove(DATA, job_id)
        raise OSError(f"文献库目录不可写：{DATA}，导入未完成。")
    if start_translation:
        _start_job(job)
    elif not _track_lifecycle:
        with _lock:
            _active_jobs.discard(job_id)
    return job


def create_job_from_path(path_value: str, pages: str, mode: str, demo_mode: bool, start_translation: bool = False) -> dict[str, Any]:
    if not isinstance(path_value, str) or not path_value.strip():
        raise ValueError("本地 PDF 路径不能为空。")
    candidate = Path(path_value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("本地 PDF 路径必须是绝对路径。")
    try:
        candidate = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("本地 PDF 不存在。") from exc
    if not candidate.is_file() or candidate.suffix.lower() != ".pdf":
        raise ValueError("本地文件必须是 PDF。")
    if candidate.stat().st_size > MAX_LOCAL_IMPORT_BYTES:
        raise ValueError("文件超过 100 MB 限制，请压缩后再试。")
    # Keep native imports stopped until the sidecar/cache decision is made;
    # otherwise create_job would start a worker before we can inspect it.
    job = create_job(candidate.name, candidate.read_bytes(), pages, mode, demo_mode, start_translation=False, _track_lifecycle=True)
    if job.get("duplicate"):
        return job
    started = False
    try:
        job["source_origin"] = "desktop-local"
        save_job(job)
        try:
            _write_external_source(job, candidate)
        except OSError:
            job["message"] = "已导入源 PDF，但无法记录原文件位置；翻译结果仍会保存在 job 目录。"
        if job.get("mode") == "full" and not demo_mode and _reuse_external_translation(job, candidate):
            save_job(job)
            _enqueue_markdown_export(job)
            return job
        if start_translation:
            job["status"] = "queued"
            job["stage"] = "queued"
            job["message"] = ""
            save_job(job)
            _start_job(job)
            started = True
        return job
    finally:
        if not started:
            with _lock:
                _active_jobs.discard(str(job.get("id", "")))


def import_job_from_path(path_value: str, mode: str = "full", pages: str = "", demo_mode: bool = False, start_translation: bool = False) -> dict[str, Any]:
    """Native desktop bridge entry point; the picker supplies the path."""
    return create_job_from_path(path_value, pages, mode, demo_mode, start_translation=start_translation)


def translate_job(job_id: str, mode: str, pages: str, demo_mode: bool) -> dict[str, Any]:
    with _lock:
        job = load_job(job_id)
        if not job:
            raise ValueError("任务不存在。")
        if job.get("status") in {"queued", "running"}:
            raise ValueError("任务正在处理中，请等待当前任务完成。")
        mode = mode if mode in {"trial", "full"} else "full"
        pages = parse_pages(pages if mode == "trial" else "", int(job["page_count"]))
        job.update({"status": "queued", "progress": None, "progress_indeterminate": True, "stage": "queued", "stage_current": 0, "stage_total": None, "mode": mode, "pages": pages, "demo_mode": bool(demo_mode), "translation_scope": None, "translation_reused": False, "translated_title": None, "title_checked": False, "error": "", "message": ""})
        save_job(job)
        _start_job(job)
        return job


def file_path(job_id: str, kind: str) -> Path | None:
    job = load_job(job_id)
    if not job:
        return None
    mapping = {"source": "source.pdf", "translated": job.get("translated_file", ""), "bilingual": job.get("bilingual_file", "")}
    filename = mapping.get(kind, "")
    if not filename:
        return None
    candidate = (JOBS / job_id / filename).resolve()
    try:
        candidate.relative_to((JOBS / job_id).resolve())
    except ValueError:
        return None
    if kind in {"translated", "bilingual"} and not _translation_is_current(job):
        return None
    return candidate if candidate.is_file() else None


def save_translation(job_id: str, destination: str | Path) -> Path:
    """Copy a verified Chinese PDF to a user-selected native destination."""
    source = file_path(job_id, "translated")
    if not source:
        raise FileNotFoundError("中文译文尚未生成。")
    target = Path(destination).expanduser().resolve(strict=False)
    if target.suffix.lower() != ".pdf":
        raise ValueError("保存文件必须是 PDF。")
    job_dir = (JOBS / job_id).resolve()
    try:
        target.relative_to(job_dir)
    except ValueError:
        pass
    else:
        raise ValueError("不能覆盖文献库内部文件，请选择其他位置。")
    if target == source.resolve() or target == (JOBS / job_id / "source.pdf").resolve():
        raise ValueError("不能覆盖文献库内部文件，请选择其他位置。")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_copy(source, target)
    except OSError as exc:
        raise OSError(f"中文译文保存失败：{exc}") from exc
    return target


def render_page(job_id: str, kind: str, page_number: int, zoom: int = 100) -> bytes:
    """Render one page for the synchronized reader, using local PyMuPDF."""
    source = file_path(job_id, kind)
    job = load_job(job_id)
    if not source or not job:
        raise FileNotFoundError("任务文件不存在。")
    if page_number < 1 or page_number > int(job.get("page_count", 0)):
        raise ValueError("页码超出范围。")
    try:
        local_runtime = ROOT / ".runtime"
        if local_runtime.is_dir() and str(local_runtime) not in os.sys.path:
            os.sys.path.insert(0, str(local_runtime))
        try:
            import pymupdf as fitz  # New PyMuPDF import name.
        except ImportError:
            import fitz  # Compatibility with older PyMuPDF releases.
    except ImportError as exc:
        raise RuntimeError("缺少 PyMuPDF，请运行启动脚本自动安装渲染依赖。") from exc
    zoom = max(50, min(200, int(zoom)))
    return _render_page_pdf(source, page_number, zoom)


@pdf_serialized
def _render_page_pdf(source: Path, page_number: int, zoom: int) -> bytes:
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    document = fitz.open(str(source))
    try:
        page = document.load_page(page_number - 1)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom / 100, zoom / 100), alpha=False)
        return pixmap.tobytes("png")
    finally:
        document.close()
