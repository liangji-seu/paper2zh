"""Project-local BabelDOC launcher used by the Windows app."""

from __future__ import annotations

import os
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
runtime = ROOT / (".runtime311" if sys.version_info[:2] == (3, 11) else ".runtime")
runtime_home = Path(os.environ.get("PAPER_TRANSLATOR_CACHE_DIR") or (Path(os.environ.get("PAPER_TRANSLATOR_DATA_DIR") or ROOT / "data") / "babeldoc-user"))
runtime_home.mkdir(parents=True, exist_ok=True)
# BabelDOC derives its cache from Path.home(). Keep model/font caches with
# this app instead of writing to a global user profile.
os.environ["USERPROFILE"] = str(runtime_home)
sys.path.insert(0, str(runtime))

import babeldoc.main as _babeldoc_main
_original_progress_handler = _babeldoc_main.create_progress_handler


def _emit_machine_progress(config, show_log=False):
    """Keep progress events parseable even when Rich wraps debug logs."""
    context, handler = _original_progress_handler(config, show_log)

    def wrapped(event):
        if isinstance(event, dict) and event.get("type") in {"progress_start", "progress_update", "progress_end"}:
            payload = {
                key: event.get(key)
                for key in ("type", "stage", "stage_current", "stage_total", "overall_progress")
                if key in event
            }
            print("PAPER_TRANSLATOR_PROGRESS " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)
        return handler(event)

    return context, wrapped


_babeldoc_main.create_progress_handler = _emit_machine_progress
cli = _babeldoc_main.cli


if __name__ == "__main__":
    cli()
