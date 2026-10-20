"""Local Claude Code bridge for the reading assistant panel.

The panel talks to the Claude Code CLI the user already installed on this
machine.  Every turn is a separate ``claude -p`` process whose stream-json
output is relayed to the browser as server-sent events; continuity between
turns comes from ``--resume <session-id>``.

Nothing here writes into the user's paper library: the assistant only gets a
read-only view of the current paper.  Derived context files live under the
application data directory (next to settings.json), never inside ``jobs/``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .core import DATA, JOBS, SETTINGS_PATH, file_path, list_jobs, load_job

AGENT_DIR = SETTINGS_PATH.parent / "agent"
WORKSPACE_DIR = AGENT_DIR / "workspace"
SESSIONS_PATH = AGENT_DIR / "sessions.json"

MAX_MESSAGE_CHARS = 8000
MAX_CONTEXT_PAGES = 300
MAX_CONTEXT_CHARS = 2_000_000
MAX_TOOL_DETAIL = 80
RUN_TIMEOUT_SECONDS = 600

ALLOWED_TOOLS = "Read,Glob,Grep"

_lock = threading.RLock()
_active: dict[str, subprocess.Popen[bytes]] = {}
_sessions_cache: dict[str, dict[str, Any]] | None = None
_claude_cache: dict[str, Any] = {}


class AgentUnavailable(RuntimeError):
    pass


def _creationflags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def claude_binary() -> str | None:
    """Locate the Claude Code executable without going through a .cmd shim."""
    override = os.environ.get("PAPER2ZH_CLAUDE_BIN", "").strip()
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    found = shutil.which("claude")
    if found:
        candidates.append(Path(found))
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
        candidates.append(Path(appdata) / "npm" / "claude.exe")
    candidates.append(Path.home() / ".local" / "bin" / "claude")
    candidates.append(Path.home() / ".claude" / "local" / "claude")
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.suffix.lower() in {"", ".exe"}:
                return str(candidate)
        except OSError:
            continue
    return None


def _version(binary: str) -> str:
    try:
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            timeout=15,
            creationflags=_creationflags(),
            env=_environment(),
        )
        text = (completed.stdout or b"").decode("utf-8", "replace").strip()
        match = re.search(r"\d+\.\d+\.\d+", text)
        return match.group(0) if match else text[:40]
    except (OSError, subprocess.SubprocessError):
        return ""


def info() -> dict[str, Any]:
    cached = _claude_cache.get("info")
    if cached and time.time() - float(_claude_cache.get("at", 0)) < 60:
        return cached
    binary = claude_binary()
    payload = {
        "available": bool(binary),
        "version": _version(binary) if binary else "",
        "allowed_tools": [tool for tool in ALLOWED_TOOLS.split(",") if tool],
        "data_dir": str(DATA),
    }
    _claude_cache["info"] = payload
    _claude_cache["at"] = time.time()
    return payload


def _environment() -> dict[str, str]:
    env = {str(key): str(value) for key, value in os.environ.items()}
    env.setdefault("NO_COLOR", "1")
    env["CLAUDE_CODE_ENTRYPOINT"] = "paper2zh"
    return env


# ---------------------------------------------------------------------------
# Context workspace


def _safe_key(job_id: str | None) -> str:
    if not job_id:
        return "__library__"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", job_id):
        raise ValueError("文献 ID 无效。")
    return job_id


def _write_if_changed(path: Path, text: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        pass
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _paper_title(job: dict[str, Any]) -> str:
    return str(job.get("translated_title") or job.get("display_title") or job.get("filename") or "未命名论文")


def _status_label(job: dict[str, Any]) -> str:
    status = str(job.get("status") or "")
    if job.get("demo_mode"):
        return "演示模式（译文为原文副本）"
    return {
        "completed": "翻译完成",
        "running": "翻译中",
        "queued": "等待翻译",
        "failed": "翻译失败",
        "imported": "仅导入原文",
    }.get(status, status or "仅导入原文")


def _extract_pdf_text(path: Path) -> str:
    """Extract page-anchored plain text with pypdf (no extra network calls)."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    lines: list[str] = []
    used = 0
    for number, page in enumerate(reader.pages, start=1):
        if number > MAX_CONTEXT_PAGES or used >= MAX_CONTEXT_CHARS:
            lines.extend(["", f"> [仅提取前 {number - 1} 页，全文共 {len(reader.pages)} 页。]"])
            break
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text).strip()
        lines.extend([f"## 第 {number} 页", "", text or "> [本页没有可提取文字，可能是扫描页。]", ""])
        used += len(text)
    return "\n".join(lines).rstrip() + "\n"


def _library_markdown(job: dict[str, Any]) -> tuple[str, str] | None:
    """Return (source.md, translated.md) from the verified export when present."""
    directory = JOBS / str(job.get("id")) / "markdown"
    source = directory / "source.md"
    translated = directory / "translated.md"
    try:
        if source.is_file() and source.stat().st_size > 0:
            return (
                source.read_text(encoding="utf-8", errors="replace"),
                translated.read_text(encoding="utf-8", errors="replace") if translated.is_file() else "",
            )
    except OSError:
        return None
    return None


def _paper_context(job: dict[str, Any]) -> tuple[str, str]:
    exported = _library_markdown(job)
    if exported:
        source_text, translated_text = exported
    else:
        source_text, translated_text = "", ""
        source_pdf = file_path(str(job.get("id")), "source")
        if source_pdf:
            try:
                source_text = _extract_pdf_text(source_pdf)
            except Exception as exc:  # corrupted or encrypted PDF
                source_text = f"> [原文提取失败：{type(exc).__name__}]"
    if len(source_text) > MAX_CONTEXT_CHARS:
        source_text = source_text[:MAX_CONTEXT_CHARS] + "\n\n> [原文过长，已截断；可按页码继续提问。]\n"
    header = [
        f"# {_paper_title(job)}",
        "",
        f"- 页数：{job.get('page_count') or '未知'}",
        f"- 状态：{_status_label(job)}",
        f"- 翻译范围：{job.get('translation_scope') or job.get('mode') or '未翻译'}",
        "",
        "> 由 paper2zh 从本地 PDF/译文导出，按页分隔，仅供当前会话阅读。",
        "",
    ]
    body = "\n".join(header) + source_text
    translation = ""
    if translated_text.strip():
        translation = (
            "# 中文译文（machine translation, 仅供参照）\n\n"
            + translated_text
            + "\n"
        )
    return body, translation


def _stat_pair(path: Path | None) -> list[int] | None:
    if not path:
        return None
    try:
        stat = path.stat()
        return [int(stat.st_mtime_ns), int(stat.st_size)]
    except OSError:
        return None


def _context_signature(job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job.get("id"))
    markdown = JOBS / job_id / "markdown"
    return {
        "title": _paper_title(job),
        "status": job.get("status"),
        "scope": job.get("translation_scope") or job.get("mode"),
        "source": _stat_pair(file_path(job_id, "source")),
        "source_md": _stat_pair(markdown / "source.md"),
        "translated_md": _stat_pair(markdown / "translated.md"),
    }


def _build_job_workspace(job: dict[str, Any]) -> Path:
    key = _safe_key(str(job.get("id")))
    directory = WORKSPACE_DIR / key
    directory.mkdir(parents=True, exist_ok=True)
    signature = _context_signature(job)
    signature_path = directory / "context.json"
    try:
        cached = json.loads(signature_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        cached = None
    if cached != signature or not (directory / "paper.md").is_file():
        paper, translation = _paper_context(job)
        _write_if_changed(directory / "paper.md", paper)
        if translation:
            _write_if_changed(directory / "translation.md", translation)
        else:
            (directory / "translation.md").unlink(missing_ok=True)
    info_text = "\n".join(
        [
            f"# {_paper_title(job)}",
            "",
            f"- 原文件名：{job.get('filename') or '未知'}",
            f"- 页数：{job.get('page_count') or '未知'}",
            f"- 状态：{_status_label(job)}",
            f"- 翻译范围：{job.get('translation_scope') or job.get('mode') or '未翻译'}",
            f"- 导入时间：{job.get('created_at') or '未知'}",
            "",
            "工作目录文件：",
            "- `paper.md`：论文原文全文，按“## 第 N 页”分页。",
            "- `translation.md`：中文译文（仅翻译后存在）。",
        ]
    )
    _write_if_changed(directory / "info.md", info_text + "\n")
    signature_path.write_text(json.dumps(signature, ensure_ascii=False), encoding="utf-8")
    return directory


def _build_library_workspace() -> Path:
    directory = WORKSPACE_DIR / "__library__"
    directory.mkdir(parents=True, exist_ok=True)
    rows = ["# 我的文献库", "", "本地文献列表（paper2zh）。当前没有打开任何论文。", "", f"文献目录：`{JOBS}`", ""]
    for job in list_jobs():
        rows.append(f"- {_paper_title(job)}（{job.get('page_count') or '?'} 页，{_status_label(job)}）")
    if len(rows) == 4:
        rows.append("> 文献库为空。")
    rows.extend(
        [
            "",
            "每篇论文的导出 Markdown 位于应用数据目录的 `jobs/<文献ID>/markdown/` 下，",
            "其中 `source.md` 是原文、`translated.md` 是中文译文（若已翻译）。",
        ]
    )
    _write_if_changed(directory / "index.md", "\n".join(rows) + "\n")
    return directory


def prepare_workspace(job_id: str | None) -> tuple[Path, dict[str, Any] | None]:
    job: dict[str, Any] | None = None
    if job_id:
        job = load_job(str(job_id))
        if not job:
            raise ValueError("文献不存在或已被删除。")
    directory = _build_job_workspace(job) if job else _build_library_workspace()
    return directory, job


# ---------------------------------------------------------------------------
# Prompts


def _system_prompt(job: dict[str, Any] | None) -> str:
    if not job:
        return (
            "你是 paper2zh 内置的文献阅读助手，运行在用户本机的 Claude Code 中。\n"
            "用户当前没有打开具体论文；工作目录里的 `index.md` 是本地文献库索引。\n"
            "回答用中文，先引用具体文件与证据，不确定就直说。不要编造文献内容。"
        )
    return (
        "你是 paper2zh 内置的文献精读助手，运行在用户本机的 Claude Code 中。\n"
        f"当前打开的论文：《{_paper_title(job)}》，共 {job.get('page_count') or '未知'} 页，{_status_label(job)}。\n"
        "\n"
        "工作目录内的文件：\n"
        "- `paper.md`：论文原文全文，按“## 第 N 页”分页；这是主要依据。\n"
        "- `translation.md`：中文译文（机器翻译，仅供参照，可能有误）。\n"
        "- `info.md`：文献元数据。\n"
        f"- 其他论文的 Markdown 位于文献目录 `{JOBS}` 下；需要时可按标题检索。\n"
        "\n"
        "回答规则：\n"
        "1. 默认用中文回答，先读文件再下结论；引用原文时标注页码，例如“第 4 页”。\n"
        "2. 区分“论文明确写了什么”和“你的推断/评价”，不要编造论文中没有的内容。\n"
        "3. 回答服务于精读：概念解释、方法拆解、公式与实验解读、批判性评价、相关工作定位。\n"
        "4. 论文较长时用 Grep 定位，再 Read 相关段落，不要一次读完整篇。\n"
        "5. 用户消息中的 `<reading-position>` 表示其当前正在阅读的页码。\n"
        "6. 如果 `paper.md` 标注正文不可提取（扫描页），直接说明这一点，不要猜测内容。\n"
    )


def _turn_prompt(message: str, page: int | None, job: dict[str, Any] | None) -> str:
    lines: list[str] = []
    if job and page:
        lines.append(f'<reading-position page="{int(page)}" total="{job.get("page_count") or "?"}"/>')
    lines.append(message)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sessions


def _load_sessions() -> dict[str, dict[str, Any]]:
    global _sessions_cache
    with _lock:
        if _sessions_cache is not None:
            return _sessions_cache
        try:
            payload = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            payload = {}
        _sessions_cache = payload if isinstance(payload, dict) else {}
        return _sessions_cache


def _save_sessions() -> None:
    sessions = _load_sessions()
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    temporary = SESSIONS_PATH.with_name(f".sessions.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, SESSIONS_PATH)


def _get_session(key: str) -> str | None:
    record = _load_sessions().get(key)
    session_id = record.get("session_id") if isinstance(record, dict) else None
    return session_id if isinstance(session_id, str) and session_id else None


def _record_session(key: str, session_id: str, cwd: Path) -> None:
    if not session_id:
        return
    with _lock:
        sessions = _load_sessions()
        sessions[key] = {
            "session_id": session_id,
            "cwd": str(cwd),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_sessions()


def reset(job_id: str | None) -> dict[str, Any]:
    key = _safe_key(job_id)
    with _lock:
        sessions = _load_sessions()
        had = sessions.pop(key, None) is not None
        if had:
            _save_sessions()
    return {"reset": had}


# ---------------------------------------------------------------------------
# Running Claude Code


def build_command(binary: str, prompt: str, cwd: Path, resume_id: str | None, system_prompt: str) -> list[str]:
    command = [
        binary,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--allowedTools",
        ALLOWED_TOOLS,
        "--strict-mcp-config",
        "--append-system-prompt",
        system_prompt,
        "--add-dir",
        str(JOBS),
    ]
    if resume_id:
        command.extend(["--resume", resume_id])
    return command


def _tool_detail(name: str, payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    if name in {"Grep", "Glob"}:
        keys = ("pattern", "glob", "file_path", "path")
    elif name == "Read":
        keys = ("file_path", "path")
    else:
        keys = ("file_path", "path", "pattern", "query", "url")
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip().replace("\\", "/").rsplit("/", 1)[-1] if key in {"file_path", "path"} else value.strip()
            return text[:MAX_TOOL_DETAIL]
    return ""


def _translate_event(event: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    """Map one stream-json record from the CLI onto panel events."""
    out: list[dict[str, Any]] = []
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            state["session_id"] = session_id
        out.append({"type": "init", "model": event.get("model") or "", "session_id": session_id or ""})
        return out
    if kind == "stream_event":
        inner = event.get("event")
        if not isinstance(inner, dict):
            return out
        inner_type = inner.get("type")
        if inner_type == "content_block_start":
            block = inner.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = str(block.get("name") or "工具")
                state["tool"] = name
                out.append({"type": "tool", "name": name, "detail": ""})
            return out
        if inner_type == "content_block_delta":
            delta = inner.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    out.append({"type": "text", "text": text})
            return out
        if inner_type == "content_block_stop":
            if state.get("tool"):
                out.append({"type": "tool_done", "name": state.pop("tool")})
            return out
        return out
    if kind == "assistant":
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = str(block.get("name") or "工具")
                    out.append({"type": "tool_detail", "name": name, "detail": _tool_detail(name, block.get("input"))})
        return out
    if kind == "result":
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        out.append(
            {
                "type": "done",
                "session_id": event.get("session_id") or state.get("session_id") or "",
                "is_error": bool(event.get("is_error")),
                "result": str(event.get("result") or "")[:2000],
                "duration_ms": event.get("duration_ms"),
                "cost_usd": event.get("total_cost_usd"),
                "usage": {
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                },
                "permission_denials": event.get("permission_denials") or [],
            }
        )
    return out


def _iter_process(
    command: list[str],
    cwd: Path,
    key: str,
    stderr_tail: list[str],
    state: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_environment(),
        creationflags=_creationflags(),
    )
    with _lock:
        _active[key] = process
    timed_out = threading.Event()

    def _kill_for_timeout() -> None:
        if process.poll() is None:
            timed_out.set()
            try:
                process.kill()
            except OSError:
                pass

    watchdog = threading.Timer(RUN_TIMEOUT_SECONDS, _kill_for_timeout)
    watchdog.daemon = True
    watchdog.start()
    stderr_thread = threading.Thread(target=_drain_stderr, args=(process, stderr_tail), daemon=True)
    stderr_thread.start()
    try:
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            for mapped in _translate_event(event, state):
                yield mapped
            if event.get("type") == "result":
                break
        if timed_out.is_set():
            raise TimeoutError("Claude Code 超时未返回，已停止。")
    finally:
        watchdog.cancel()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        stderr_thread.join(timeout=2)
        with _lock:
            _active.pop(key, None)


def _drain_stderr(process: subprocess.Popen[bytes], sink: list[str]) -> None:
    stream = process.stderr
    if stream is None:
        return
    for raw in stream:
        text = raw.decode("utf-8", "replace").strip()
        if text:
            sink.append(text[:400])
            del sink[:-8]


def _friendly_error(stderr: list[str]) -> str:
    joined = "\n".join(stderr)
    if "No conversation found" in joined:
        return "上一轮会话已失效，请重试。"
    if "not logged in" in joined.lower() or "authentication" in joined.lower():
        return "Claude Code 未登录或认证失败，请先在终端运行 claude 完成登录。"
    if "permission" in joined.lower() and "denied" in joined.lower():
        return "Claude Code 拒绝了工具权限；本面板只允许读取，不接受写入。"
    tail = stderr[-1] if stderr else ""
    return f"Claude Code 运行失败：{tail[:200]}" if tail else "Claude Code 没有返回内容。"


def stream_chat(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield panel events for one user turn."""
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        yield {"type": "error", "message": "请输入问题。"}
        return
    if len(message) > MAX_MESSAGE_CHARS:
        yield {"type": "error", "message": f"问题过长，请控制在 {MAX_MESSAGE_CHARS} 字以内。"}
        return
    job_id = payload.get("job_id")
    job_id = str(job_id) if isinstance(job_id, str) and job_id else None
    try:
        page = int(payload.get("page") or 0) or None
    except (TypeError, ValueError):
        page = None
    try:
        key = _safe_key(job_id)
        cwd, job = prepare_workspace(job_id)
    except (ValueError, OSError) as exc:
        yield {"type": "error", "message": str(exc)}
        return
    binary = claude_binary()
    if not binary:
        yield {"type": "error", "message": "未检测到 Claude Code，请先安装并登录 Claude Code CLI。"}
        return

    system_prompt = _system_prompt(job)
    prompt = _turn_prompt(message.strip(), page, job)
    yield {"type": "ready"}

    stderr: list[str] = []
    session_id = _get_session(key)
    for attempt in range(2):
        emitted = False
        result_event: dict[str, Any] | None = None
        used_resume = bool(session_id)
        command = build_command(binary, prompt, cwd, session_id, system_prompt)
        state: dict[str, Any] = {}
        try:
            for event in _iter_process(command, cwd, key, stderr, state):
                if event.get("type") == "init" and event.get("session_id"):
                    session_id = str(event["session_id"])
                if event.get("type") == "text":
                    emitted = True
                if event.get("type") == "done":
                    result_event = event
                    if event.get("session_id"):
                        session_id = str(event["session_id"])
                yield event
        except TimeoutError as exc:
            yield {"type": "error", "message": str(exc)}
            return
        except OSError as exc:
            yield {"type": "error", "message": f"无法启动 Claude Code：{exc}"}
            return

        if session_id:
            _record_session(key, session_id, cwd)
        if result_event is not None:
            if result_event.get("is_error") and not emitted:
                yield {"type": "error", "message": _friendly_error(stderr) if stderr else "Claude Code 返回错误，请重试。"}
            return
        if used_resume and not emitted:
            # Stale session id: forget it and retry once from a clean session.
            reset(job_id)
            session_id = None
            stderr.clear()
            yield {"type": "notice", "message": "会话已重新开始。"}
            continue
        if not emitted:
            yield {"type": "error", "message": _friendly_error(stderr)}
        return


def cancel(job_id: str | None) -> dict[str, Any]:
    try:
        key = _safe_key(job_id)
    except ValueError:
        return {"cancelled": False}
    with _lock:
        process = _active.get(key)
    if not process or process.poll() is not None:
        return {"cancelled": False}
    try:
        process.kill()
        return {"cancelled": True}
    except OSError:
        return {"cancelled": False}
