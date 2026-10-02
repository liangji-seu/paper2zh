from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pypdf import PdfReader

from .security import mask, protect, unprotect


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JOBS = DATA / "jobs"
SETTINGS_PATH = DATA / "settings.json"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

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
        args = command + ["--files", str(source), "--config", str(config_path)]
        if urlparse(settings["base_url"]).hostname == "api.deepseek.com":
            # DeepSeek V4 defaults to thinking mode. Its reasoning can consume
            # the response budget before BabelDOC receives usable text/JSON.
            args += ["--openai-thinking", "disabled", "--no-send-temperature"]
        if job.get("pages"):
            args += ["--pages", job["pages"]]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60 * 60, env=env)
    finally:
        if config_path:
            config_path.unlink(missing_ok=True)
    diagnostic = (completed.stdout or "") + "\n" + (completed.stderr or "")
    if settings.get("api_key"):
        diagnostic = diagnostic.replace(settings["api_key"], "[REDACTED]")
    diagnostic = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", diagnostic)
    (job_dir / "engine.log").write_text(diagnostic, encoding="utf-8")
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
    # file_path() serves files from the job root, while BabelDOC writes into
    # its output directory. Publish both PDFs there before completing the job.
    published_translated = job_dir / translated.name
    published_bilingual = job_dir / bilingual.name
    shutil.copy2(translated, published_translated)
    if bilingual != translated:
        shutil.copy2(bilingual, published_bilingual)
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
    translated = JOBS / job["id"] / "translated-demo.pdf"
    bilingual = JOBS / job["id"] / "bilingual-demo.pdf"
    shutil.copyfile(source, translated)
    shutil.copyfile(source, bilingual)
    return translated, bilingual


def _worker(job: dict[str, Any]) -> None:
    engine_started = False
    try:
        job["status"] = "running"
        job["progress"] = 8
        save_job(job)
        settings = read_settings()
        if job["demo_mode"]:
            translated, bilingual = _run_demo(job)
        else:
            if not settings["api_key"]:
                raise RuntimeError("尚未设置 API Key。请先在设置页保存密钥，或勾选“无 Key 演示模式”验证上传与阅读流程。")
            job["progress"] = 15
            save_job(job)
            engine_started = True
            translated, bilingual = _run_babeldoc(job, settings)
        job["translated_file"] = translated.name
        job["bilingual_file"] = bilingual.name
        job["status"] = "completed"
        job["progress"] = 100
        job["message"] = "演示模式：PDF 已保留用于阅读流程验证，未执行真实翻译。" if job["demo_mode"] else "翻译完成。"
    except Exception as exc:
        job["status"] = "failed"
        job["progress"] = 100
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
    job = {"id": job_id, "filename": safe_filename(filename), "created_at": datetime.now(timezone.utc).isoformat(), "status": "queued" if start_translation else "imported", "progress": 0, "mode": actual_mode, "pages": pages, "page_count": page_count, "demo_mode": bool(demo_mode), "error": "", "message": "已导入源 PDF，可先预览或开始翻译。" if not start_translation else ""}
    save_job(job)
    if start_translation:
        _start_job(job)
    return job


def translate_job(job_id: str, mode: str, pages: str, demo_mode: bool) -> dict[str, Any]:
    job = load_job(job_id)
    if not job:
        raise ValueError("任务不存在。")
    if job.get("status") in {"queued", "running"}:
        raise ValueError("任务正在处理中，请等待当前任务完成。")
    mode = mode if mode in {"trial", "full"} else "full"
    pages = parse_pages(pages if mode == "trial" else "", int(job["page_count"]))
    job.update({"status": "queued", "progress": 0, "mode": mode, "pages": pages, "demo_mode": bool(demo_mode), "error": "", "message": ""})
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
