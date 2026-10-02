"""Small, dependency-free parser for BabelDOC progress events."""

from __future__ import annotations

import ast
import json
import re
from typing import Any

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_EVENT_TYPES = {"progress_start", "progress_update", "progress_end"}


def _mapping_from_line(line: str) -> dict[str, Any] | None:
    clean = _ANSI.sub("", line).strip()
    for start in (clean.find("{"), clean.find("[")):
        if start < 0:
            continue
        candidate = clean[start:]
        try:
            value = json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            try:
                value = ast.literal_eval(candidate)
            except (ValueError, SyntaxError):
                continue
        if isinstance(value, dict):
            return value
    return None


def parse_engine_progress(line: str) -> dict[str, Any] | None:
    """Return a normalized event only when BabelDOC supplied real progress."""
    event = _mapping_from_line(line)
    if not event or event.get("type") not in _EVENT_TYPES:
        return None
    stage = event.get("stage")
    if not isinstance(stage, str) or not stage.strip():
        return None
    result: dict[str, Any] = {"stage": stage.strip(), "event_type": event["type"]}
    for key in ("stage_current", "stage_total"):
        value = event.get(key)
        if isinstance(value, int) and value >= 0:
            result[key] = value
    overall = event.get("overall_progress")
    if isinstance(overall, (int, float)) and 0 <= float(overall) <= 100:
        result["progress"] = round(float(overall), 2)
        result["indeterminate"] = False
    else:
        # A stage-level end is not the end of the whole document. BabelDOC
        # only gives us a reliable document percentage when it includes the
        # explicit overall_progress field; the worker marks 100 on success.
        result["progress"] = None
        result["indeterminate"] = True
    return result
