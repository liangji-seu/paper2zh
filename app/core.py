from __future__ import annotations

import json
import hashlib
import os
import queue
import re
import shutil
import subprocess
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


ROOT = Path(__file__).resolve().parent.parent
_configured_data = os.environ.get("PAPER_TRANSLATOR_DATA_DIR", "").strip()
DATA = Path(_configured_data).expanduser() if _configured_data else ROOT / "data"
JOBS = DATA / "jobs"
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


def ensure_dirs() -> None:
    DATA.mkdir(exist_ok=True)
    JOBS.mkdir(exist_ok=True)


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


def safe_filename(name: str) -> str:
    name = Path(name or "paper.pdf").name
    name = re.sub(r"[^\w.()\- 一-龥]+", "_", name).strip(" .")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:180] or "paper.pdf"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
        if job_id in _jobs:
            return dict(_jobs[job_id])
    path = JOBS / job_id / "job.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_job(job: dict[str, Any]) -> None:
    job_dir = JOBS / job["id"]
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    with _lock:
        _jobs[job["id"]] = dict(job)


def list_jobs() -> list[dict[str, Any]]:
    ensure_dirs()
    result = []
    for path in JOBS.glob("*/job.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            result.append({k: job.get(k) for k in ("id", "filename", "created_at", "status", "mode", "page_count", "error", "demo_mode")})
        except json.JSONDecodeError:
            continue
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
                probe = subprocess.run([py_launcher, "-3.11", "-c", "import sys; raise SystemExit(sys.version_info[:2] != (3, 11))"], capture_output=True, timeout=10)
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


def _run_babeldoc(job: dict[str, Any], settings: dict[str, Any]) -> tuple[Path, Path]:
    command = _find_babeldoc()
    if not command:
        raise RuntimeError("未找到 BabelDOC。请按 README 安装 BabelDOC，或在设置中确认本地环境已提供 babeldoc 命令。无 API Key 时可切换为演示模式验证流程。")
    job_dir = JOBS / job["id"]
    output_dir = job_dir / "babeldoc-output"
    output_dir.mkdir(exist_ok=True)
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
        args = command + ["--files", str(source), "--config", str(config_path), "--debug"]
        if urlparse(settings["base_url"]).hostname == "api.deepseek.com":
            # DeepSeek V4 defaults to thinking mode. Its reasoning can consume
            # the response budget before BabelDOC receives usable text/JSON.
            args += ["--openai-thinking", "disabled", "--no-send-temperature"]
        if job.get("pages"):
            args += ["--pages", job["pages"]]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, env=env)
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
    pdfs = list(output_dir.rglob("*.pdf"))
    if not pdfs:
        raise RuntimeError(f"BabelDOC 未生成 PDF：{details}")
    bilingual = next((p for p in pdfs if any(s in p.name.lower() for s in ("dual", "bilingual", "双语"))), None)
    translated = next((p for p in pdfs if any(s in p.name.lower() for s in ("mono", "translate", "translated", "译"))), None)
    bilingual = bilingual or pdfs[0]
    translated = translated or pdfs[-1]
    published_translated, published_bilingual = _publish_translation_outputs(job, translated, bilingual)
    _validate_translation_output(source, published_translated, job)
    return published_translated, published_bilingual


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
            warning = _publish_external_full_outputs(job, translated, bilingual)
            if warning:
                job["message"] = f"{job.get('message', '翻译完成。')} {warning}"
            job.update({"status": "completed", "progress": 100, "progress_indeterminate": False, "stage": "reused", "message": "已复用同源全文译文，未再次调用 API。", "error": ""})
            if warning:
                job["message"] = f"已复用同源全文译文，未再次调用 API。{warning}"
            save_job(job)
            return
        settings = read_settings()
        if job["demo_mode"]:
            translated, bilingual = _run_demo(job)
            job.update({"progress": max(float(job.get("progress") or 0), 95.0), "progress_indeterminate": False, "stage": "publishing"})
        else:
            if not settings["api_key"]:
                raise RuntimeError("尚未设置 API Key。请先在设置页保存密钥，或勾选“无 Key 演示模式”验证上传与阅读流程。")
            job["stage"] = "engine"
            save_job(job)
            engine_started = True
            translated, bilingual = _run_babeldoc(job, settings)
        external_warning = _publish_external_full_outputs(job, translated, bilingual) if job.get("mode") == "full" and not job.get("demo_mode") else None
        job["translated_file"] = translated.relative_to(JOBS / job["id"]).as_posix()
        job["bilingual_file"] = bilingual.relative_to(JOBS / job["id"]).as_posix()
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
    if engine_started:
        _record_engine_usage(job)
    save_job(job)


def _start_job(job: dict[str, Any]) -> None:
    thread = threading.Thread(target=_worker, args=(job,), daemon=True)
    thread.start()


def create_job(filename: str, content: bytes, pages: str, mode: str, demo_mode: bool, start_translation: bool = True) -> dict[str, Any]:
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("文件超过 100 MB 限制，请压缩后再试。")
    if not content.startswith(b"%PDF"):
        raise ValueError("上传内容不是有效 PDF 文件。")
    job_id = uuid.uuid4().hex
    job_dir = JOBS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    source = job_dir / "source.pdf"
    source.write_bytes(content)
    try:
        reader = PdfReader(str(source))
        page_count = len(reader.pages)
        pages = parse_pages(pages, page_count)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise ValueError("无法读取 PDF 页数，请确认文件未损坏且未加密。")
    actual_mode = mode if mode in {"trial", "full"} else "full"
    job = {"id": job_id, "filename": safe_filename(filename), "created_at": datetime.now(timezone.utc).isoformat(), "status": "queued" if start_translation else "imported", "progress": None, "progress_indeterminate": True, "stage": "queued" if start_translation else "imported", "stage_current": 0, "stage_total": None, "mode": actual_mode, "pages": pages, "page_count": page_count, "demo_mode": bool(demo_mode), "source_sha256": _sha256(source), "translation_scope": None, "translation_reused": False, "error": "", "message": "已导入源 PDF，可先预览或开始翻译。" if not start_translation else ""}
    save_job(job)
    if start_translation:
        _start_job(job)
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
    job = create_job(candidate.name, candidate.read_bytes(), pages, mode, demo_mode, start_translation=False)
    job["source_origin"] = "desktop-local"
    save_job(job)
    try:
        _write_external_source(job, candidate)
    except OSError:
        job["message"] = "已导入源 PDF，但无法记录原文件位置；翻译结果仍会保存在 job 目录。"
    if job.get("mode") == "full" and not demo_mode and _reuse_external_translation(job, candidate):
        save_job(job)
        return job
    if start_translation:
        job["status"] = "queued"
        job["stage"] = "queued"
        job["message"] = ""
        save_job(job)
        _start_job(job)
    return job


def import_job_from_path(path_value: str, mode: str = "full", pages: str = "", demo_mode: bool = False, start_translation: bool = False) -> dict[str, Any]:
    """Native desktop bridge entry point; the picker supplies the path."""
    return create_job_from_path(path_value, pages, mode, demo_mode, start_translation=start_translation)


def translate_job(job_id: str, mode: str, pages: str, demo_mode: bool) -> dict[str, Any]:
    job = load_job(job_id)
    if not job:
        raise ValueError("任务不存在。")
    if job.get("status") in {"queued", "running"}:
        raise ValueError("任务正在处理中，请等待当前任务完成。")
    mode = mode if mode in {"trial", "full"} else "full"
    pages = parse_pages(pages if mode == "trial" else "", int(job["page_count"]))
    job.update({"status": "queued", "progress": None, "progress_indeterminate": True, "stage": "queued", "stage_current": 0, "stage_total": None, "mode": mode, "pages": pages, "demo_mode": bool(demo_mode), "translation_scope": None, "translation_reused": False, "error": "", "message": ""})
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
    document = fitz.open(str(source))
    try:
        page = document.load_page(page_number - 1)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom / 100, zoom / 100), alpha=False)
        return pixmap.tobytes("png")
    finally:
        document.close()
