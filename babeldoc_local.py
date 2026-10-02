"""Project-local BabelDOC launcher used by the Windows app."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
runtime = ROOT / (".runtime311" if sys.version_info[:2] == (3, 11) else ".runtime")
runtime_home = ROOT / "data" / "babeldoc-user"
runtime_home.mkdir(parents=True, exist_ok=True)
# BabelDOC derives its cache from Path.home(). Keep model/font caches with
# this app instead of writing to a global user profile.
os.environ["USERPROFILE"] = str(runtime_home)
sys.path.insert(0, str(runtime))

from babeldoc.main import cli


if __name__ == "__main__":
    cli()
