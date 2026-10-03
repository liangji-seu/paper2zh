"""paper2zh desktop window and trusted native PDF picker."""

from __future__ import annotations

import os
import ctypes
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
    engine = resource_root() / "engine" / "python.exe"
    if engine.is_file():
        os.environ["PAPER_TRANSLATOR_PYTHON311"] = str(engine)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    os.environ["PAPER_TRANSLATOR_PORT"] = str(port)
    return port


class DesktopBridge:
    def __init__(self) -> None:
        # pywebview recursively exposes public attributes to JavaScript.
        # Keep the Window private or it traverses native WinForms/COM objects.
        self._window = None

    def import_pdf(self, options: dict | None = None) -> dict:
        """The only path-based import comes from this native user file selection."""
        import webview
        from app.core import import_job_from_path

        options = options if isinstance(options, dict) else {}
        if self._window is None:
            return {"error": "桌面窗口尚未就绪。"}
        chosen = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=False,
            file_types=("PDF 文件 (*.pdf)",),
        )
        if not chosen:
            return {"cancelled": True}
        mode = options.get("mode", "full")
        pages = options.get("pages", "")
        action = options.get("action", "import")
        if mode not in {"trial", "full"} or action not in {"import", "translate", "demo"} or not isinstance(pages, str):
            return {"error": "导入选项无效。"}
        try:
            job = import_job_from_path(
                str(chosen[0]),
                mode="full" if action == "import" else mode,
                pages=pages if mode == "trial" and action != "import" else "",
                demo_mode=action == "demo",
                start_translation=action != "import",
            )
            return {"job": {"id": job["id"], "duplicate": bool(job.get("duplicate")), "message": job.get("message", "")}}
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}

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
        raise RuntimeError("本地阅读服务启动失败。")
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
