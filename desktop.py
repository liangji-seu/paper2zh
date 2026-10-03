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
    data = Path(os.environ.get("PAPER_TRANSLATOR_DATA_DIR") or Path(os.environ["LOCALAPPDATA"]) / "paper2zh")
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
            return {"job": {"id": job["id"]}}
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
