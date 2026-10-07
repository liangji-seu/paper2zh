"""paper2zh desktop window and trusted native PDF picker."""

from __future__ import annotations

import os
import ctypes
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def configure() -> int:
    appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "paper2zh"
    packaged = bool(getattr(sys, "_MEIPASS", None))
    if packaged:
        data = Path(sys.executable).resolve().parent / "library"
        os.environ.setdefault("PAPER_TRANSLATOR_LEGACY_DATA_DIR", str(appdata))
        os.environ.setdefault("PAPER_TRANSLATOR_SETTINGS_PATH", str(appdata / "settings.json"))
        os.environ.setdefault("PAPER_TRANSLATOR_CACHE_DIR", str(appdata / "babeldoc-user"))
    else:
        data = Path(os.environ.get("PAPER_TRANSLATOR_DATA_DIR") or appdata)
        os.environ.setdefault("PAPER_TRANSLATOR_SETTINGS_PATH", str(data / "settings.json"))
    data.mkdir(parents=True, exist_ok=True)
    os.environ["PAPER_TRANSLATOR_DATA_DIR"] = str(data)
    from app.diagnostics import configure as configure_diagnostics
    configure_diagnostics(data)
    engine = resource_root() / "engine" / "python.exe"
    if engine.is_file():
        os.environ["PAPER_TRANSLATOR_PYTHON311"] = str(engine)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    os.environ["PAPER_TRANSLATOR_PORT"] = str(port)
    return port


def _copy_text_to_windows_clipboard(text: str) -> None:
    """Put UTF-16 text into the native clipboard without shelling out."""
    if sys.platform != "win32":
        raise RuntimeError("Markdown 目录复制仅支持 Windows 原生桌面桥。")
    if not isinstance(text, str):
        raise TypeError("剪贴板内容无效。")

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    size_t = ctypes.c_size_t
    void_p = ctypes.c_void_p
    bool_t = ctypes.c_bool
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, size_t]
    kernel32.GlobalAlloc.restype = void_p
    kernel32.GlobalLock.argtypes = [void_p]
    kernel32.GlobalLock.restype = void_p
    kernel32.GlobalUnlock.argtypes = [void_p]
    kernel32.GlobalUnlock.restype = bool_t
    kernel32.GlobalFree.argtypes = [void_p]
    kernel32.GlobalFree.restype = void_p
    user32.OpenClipboard.argtypes = [void_p]
    user32.OpenClipboard.restype = bool_t
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = bool_t
    user32.SetClipboardData.argtypes = [ctypes.c_uint, void_p]
    user32.SetClipboardData.restype = void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = bool_t
    user32.CreateWindowExW.argtypes = [
        ctypes.c_uint,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        void_p,
        void_p,
        void_p,
        void_p,
    ]
    user32.CreateWindowExW.restype = void_p
    user32.DestroyWindow.argtypes = [void_p]
    user32.DestroyWindow.restype = bool_t
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetModuleHandleW.restype = void_p

    # CF_UNICODETEXT expects a movable global allocation containing a trailing
    # UTF-16 NUL. Windows takes ownership only after SetClipboardData succeeds.
    payload = (text + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(0x0002, len(payload))  # GMEM_MOVEABLE
    if not handle:
        raise OSError("Windows 无法分配剪贴板内存。")
    # A message-only owner gives OpenClipboard a real HWND even when the
    # bridge is called from a worker thread without an active webview window.
    owner = user32.CreateWindowExW(
        0,
        "STATIC",
        "paper2zh clipboard owner",
        0,
        0,
        0,
        0,
        0,
        ctypes.c_void_p(-3),  # HWND_MESSAGE
        None,
        kernel32.GetModuleHandleW(None),
        None,
    )
    if not owner:
        kernel32.GlobalFree(handle)
        raise OSError("无法创建 Windows 剪贴板临时窗口。")
    opened = False
    transferred = False
    try:
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise OSError("Windows 无法锁定剪贴板内存。")
        try:
            ctypes.memmove(pointer, payload, len(payload))
        finally:
            kernel32.GlobalUnlock(handle)

        # Another process may briefly own the clipboard. Retry only for a
        # short bounded interval so copying never blocks the UI for long.
        for attempt in range(4):
            if user32.OpenClipboard(owner):
                opened = True
                break
            if attempt < 3:
                time.sleep(0.05)
        if not opened:
            raise OSError("Windows 剪贴板正忙，请稍后重试。")
        if not user32.EmptyClipboard():
            raise OSError("无法清空 Windows 剪贴板。")
        if not user32.SetClipboardData(13, handle):  # CF_UNICODETEXT
            raise OSError("无法写入 Windows 剪贴板。")
        transferred = True
    finally:
        if opened:
            user32.CloseClipboard()
        user32.DestroyWindow(owner)
        if not transferred:
            kernel32.GlobalFree(handle)


class DesktopBridge:
    def __init__(self) -> None:
        # pywebview recursively exposes public attributes to JavaScript.
        # Keep the Window private or it traverses native WinForms/COM objects.
        self._window = None
        self._import_lock = threading.Lock()

    @staticmethod
    def _import_error(exc: Exception) -> str:
        """Keep per-file errors useful without echoing local paths."""
        message = str(exc).strip()
        if not message:
            return "导入失败。"
        # Core errors are normally already user-facing. Redact path-shaped
        # fragments in case a platform/library error includes a local path.
        message = re.sub(r"[A-Za-z]:[\\/][^;，。；]+", "[本地路径]", message)
        message = re.sub(r"(?<![A-Za-z0-9])[\\/]{2}[^;，。；]+", "[本地路径]", message)
        return message

    def import_pdf(self, options: dict | None = None) -> dict:
        """The only path-based import comes from this native user file selection."""
        options = options if isinstance(options, dict) else {}
        mode = options.get("mode", "full")
        pages = options.get("pages", "")
        action = options.get("action", "import")
        if mode not in {"trial", "full"} or action not in {"import", "translate", "demo"} or not isinstance(pages, str):
            return {"error": "导入选项无效。"}
        if self._window is None:
            return {"error": "桌面窗口尚未就绪。"}
        if not self._import_lock.acquire(blocking=False):
            return {"error": "已有导入正在进行，请稍候。"}
        try:
            import webview
            from app.core import import_job_from_path

            chosen = self._window.create_file_dialog(
                webview.FileDialog.OPEN,
                allow_multiple=True,
                file_types=("PDF 文件 (*.pdf)",),
            )
            if not chosen:
                return {"cancelled": True}
            paths = tuple(path for path in chosen if isinstance(path, str) and path)
            if not paths:
                return {"error": "文件选择结果无效。"}
            if len(paths) > 1 and action != "import":
                return {"error": "批量导入请使用‘仅导入，稍后翻译’，导入并翻译/演示暂仅支持单篇"}

            if len(paths) == 1:
                job = import_job_from_path(
                    paths[0],
                    mode="full" if action == "import" else mode,
                    pages=pages if mode == "trial" and action != "import" else "",
                    demo_mode=action == "demo",
                    start_translation=action != "import",
                )
                return {"job": {"id": job["id"], "duplicate": bool(job.get("duplicate")), "message": job.get("message", "")}}

            results = []
            for path in paths:
                filename = Path(path).name
                try:
                    job = import_job_from_path(path, mode="full", pages="", demo_mode=False, start_translation=False)
                except Exception as exc:
                    results.append({"filename": filename, "error": self._import_error(exc)})
                else:
                    duplicate = bool(job.get("duplicate"))
                    results.append({
                        "filename": filename,
                        "job": {"id": job["id"], "duplicate": duplicate, "message": job.get("message", "")},
                        "duplicate": duplicate,
                    })
            return {"results": results, "total": len(paths)}
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}
        finally:
            self._import_lock.release()

    def save_translation(self, job_id: str) -> dict:
        """Native Save As for a verified translated PDF only."""
        import webview
        from app.core import file_path, load_job, safe_filename, save_translation as save_translation_file

        if self._window is None:
            return {"error": "桌面窗口尚未就绪。"}
        if not isinstance(job_id, str) or not job_id:
            return {"error": "任务 id 无效。"}
        job = load_job(job_id)
        if not job or not file_path(job_id, "translated"):
            return {"error": "中文译文尚未生成。"}
        title = job.get("translated_title") or job.get("display_title") or job.get("filename") or "paper"
        if not job.get("translated_title") and str(title).lower().endswith(".pdf"):
            title = Path(str(title)).stem
        default_name = safe_filename(f"{title}.zh.pdf")
        chosen = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=default_name, file_types=("中文 PDF (*.pdf)",))
        if not chosen:
            return {"cancelled": True}
        try:
            saved = save_translation_file(job_id, str(chosen[0]))
            return {"saved": True, "filename": saved.name}
        except (ValueError, OSError, FileNotFoundError) as exc:
            return {"error": str(exc)}

    def save_diagnostics(self) -> dict:
        """Native Save As for the small, privacy-filtered diagnostics bundle."""
        import webview
        from app.diagnostics import export_bytes

        if self._window is None:
            return {"error": "桌面窗口尚未就绪。"}
        chosen = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename="paper2zh-diagnostics.zip",
            file_types=("诊断日志 ZIP (*.zip)",),
        )
        if not chosen:
            return {"cancelled": True}
        destination = Path(str(chosen[0]))
        temporary = destination.with_name(f".{destination.name}.tmp")
        try:
            temporary.write_bytes(export_bytes())
            os.replace(temporary, destination)
            return {"saved": True, "filename": destination.name}
        except (OSError, TypeError, ValueError):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return {"error": "诊断日志保存失败，请选择可写的位置。"}

    def copy_markdown_directory(self, job_id: str) -> dict:
        """Copy only the verified Markdown directory for a known job."""
        if not isinstance(job_id, str) or not job_id:
            return {"error": "任务 id 无效。"}
        try:
            from app.core import JOBS
            from app.markdown_export import MarkdownExportError, markdown_directory

            directory = markdown_directory(JOBS, job_id)
        except MarkdownExportError as exc:
            return {"error": str(exc)}
        except (OSError, ValueError) as exc:
            return {"error": f"Markdown 目录不可用：{exc}"}
        try:
            _copy_text_to_windows_clipboard(str(directory))
        except (OSError, RuntimeError, TypeError) as exc:
            return {"error": str(exc)}
        return {"copied": True, "directory": str(directory)}

    def get_workspace_preferences(self) -> dict:
        from app.core import get_workspace_preferences
        return get_workspace_preferences()

    def save_workspace_preferences(self, values: dict | None = None) -> dict:
        from app.core import save_workspace_preferences
        try:
            return save_workspace_preferences(values if isinstance(values, dict) else {})
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}


def main() -> None:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    port = configure()
    from app.diagnostics import install_exception_hooks, record as diagnostic, record_exception as diagnostic_exception
    install_exception_hooks()
    diagnostic("desktop_start", fields={"phase": "startup"})
    from app.server import run
    import webview

    # Keep the taskbar identity stable across upgrades; WinForms uses the ICO
    # supplied to webview.start for the window, and the EXE embeds the same ICO.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("paper2zh.desktop")

    threading.Thread(target=run, daemon=True, name="paper2zh-local-server").start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=0.5):
                break
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.1)
    else:
        error = RuntimeError("本地阅读服务启动失败。")
        diagnostic_exception("desktop_start_failed", error, fields={"phase": "health_check"})
        raise error
    bridge = DesktopBridge()
    bridge._window = webview.create_window(
        "paper2zh · 论文工作区",
        url,
        js_api=bridge,
        width=1440,
        height=900,
        min_size=(920, 600),
        text_select=True,
    )
    webview.start(gui="edgechromium", icon=str(resource_root() / "static" / "icon.ico"), private_mode=True)


if __name__ == "__main__":
    main()
