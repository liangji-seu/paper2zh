from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import agent


class AgentCommandTests(unittest.TestCase):
    def test_command_is_read_only_and_never_edits(self):
        command = agent.build_command("claude.exe", "你好", Path("C:/tmp"), None, "sys")
        self.assertEqual(command[0], "claude.exe")
        self.assertEqual(command[command.index("-p") + 1], "你好")
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(command[command.index("--allowedTools") + 1], "Read,Glob,Grep")
        self.assertIn("--strict-mcp-config", command)
        self.assertNotIn("--resume", command)
        self.assertNotIn("--dangerously-skip-permissions", command)

    def test_command_resumes_existing_session(self):
        command = agent.build_command("claude.exe", "hi", Path("C:/tmp"), "1234", "sys")
        self.assertEqual(command[command.index("--resume") + 1], "1234")

    def test_invalid_job_id_is_rejected(self):
        with self.assertRaises(ValueError):
            agent._safe_key("../etc")
        self.assertEqual(agent._safe_key(None), "__library__")


class AgentEventTests(unittest.TestCase):
    def test_text_delta_becomes_panel_text(self):
        state = {}
        events = agent._translate_event(
            {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "第 2 页"}}},
            state,
        )
        self.assertEqual(events, [{"type": "text", "text": "第 2 页"}])

    def test_tool_use_emits_start_detail_and_done(self):
        state = {}
        start = agent._translate_event(
            {"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}}},
            state,
        )
        self.assertEqual(start[0]["type"], "tool")
        self.assertEqual(start[0]["name"], "Read")
        detail = agent._translate_event(
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "C:/ws/paper.md"}}]}},
            state,
        )
        self.assertEqual(detail[0], {"type": "tool_detail", "name": "Read", "detail": "paper.md"})
        grep = agent._translate_event(
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Grep", "input": {"pattern": "sEMG", "path": "C:/ws"}}]}},
            state,
        )
        self.assertEqual(grep[0]["detail"], "sEMG")
        done = agent._translate_event({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}, state)
        self.assertEqual(done, [{"type": "tool_done", "name": "Read"}])

    def test_result_carries_usage_and_session(self):
        events = agent._translate_event(
            {"type": "result", "session_id": "s1", "is_error": False, "result": "ok", "duration_ms": 5, "total_cost_usd": 0.1, "usage": {"input_tokens": 10, "output_tokens": 2}},
            {},
        )
        self.assertEqual(events[0]["type"], "done")
        self.assertEqual(events[0]["session_id"], "s1")
        self.assertEqual(events[0]["usage"]["input_tokens"], 10)


class AgentContextTests(unittest.TestCase):
    def _workspace(self, root: Path, job_id: str = "job-1") -> Path:
        markdown = root / "jobs" / job_id / "markdown"
        markdown.mkdir(parents=True)
        (markdown / "source.md").write_text("# Paper\n\n## 第 1 页\n\nOriginal body.\n", encoding="utf-8")
        (markdown / "translated.md").write_text("# 论文\n\n## 第 1 页\n\n中文正文。\n", encoding="utf-8")
        (markdown / "notes.md").write_text("# 我的备忘\n\n重点：融合 sEMG。\n", encoding="utf-8")
        return markdown

    def test_workspace_contains_page_anchored_source_and_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._workspace(root)
            job = {"id": "job-1", "filename": "paper.pdf", "page_count": 3, "status": "completed", "translated_title": "示例论文"}
            with patch.object(agent, "JOBS", root / "jobs"), patch.object(agent, "WORKSPACE_DIR", root / "agent"), patch.object(agent, "load_job", return_value=job), patch.object(agent, "file_path", return_value=None):
                cwd, loaded = agent.prepare_workspace("job-1")
                self.assertEqual(loaded["id"], "job-1")
                paper = (cwd / "paper.md").read_text(encoding="utf-8")
                self.assertIn("Original body.", paper)
                self.assertIn("## 第 1 页", paper)
                self.assertIn("示例论文", paper)
                self.assertIn("中文正文", (cwd / "translation.md").read_text(encoding="utf-8"))
                self.assertIn("融合 sEMG", (cwd / "notes.md").read_text(encoding="utf-8"))
                info = (cwd / "info.md").read_text(encoding="utf-8")
                self.assertIn("paper.pdf", info)
                first = (cwd / "paper.md").stat().st_mtime_ns
                agent.prepare_workspace("job-1")
                self.assertEqual((cwd / "paper.md").stat().st_mtime_ns, first)

    def test_missing_job_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(agent, "WORKSPACE_DIR", Path(directory)), patch.object(agent, "load_job", return_value=None):
                with self.assertRaises(ValueError):
                    agent.prepare_workspace("missing")

    def test_system_prompt_describes_paper_files(self):
        prompt = agent._system_prompt({"id": "j", "filename": "a.pdf", "page_count": 8, "status": "completed", "translated_title": "标题"})
        self.assertIn("paper.md", prompt)
        self.assertIn("标题", prompt)
        self.assertIn("页码", prompt)


if __name__ == "__main__":
    unittest.main()
