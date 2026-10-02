import sys
from pathlib import Path

LOCAL_RUNTIME = Path(__file__).resolve().parent / ".runtime"
if LOCAL_RUNTIME.is_dir():
    sys.path.insert(0, str(LOCAL_RUNTIME))

from app.server import run


if __name__ == "__main__":
    run()
