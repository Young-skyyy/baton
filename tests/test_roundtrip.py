import json
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from baton import bundle, conflicts, redact, render, snapshot
from baton.sources import codex

TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp"


def _tmpdir() -> Path:
    path = TMP_ROOT / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


def _rollout(path: Path) -> Path:
    lines = [
        {"type": "session_meta", "payload": {"id": "abc-123", "cwd": "C:/work"}},
        {
            "type": "response_item",
            "timestamp": "2026-11-01T10:00:00Z",
            "payload": {"type": "message", "role": "user", "content": [{"text": "帮我修耳机"}]},
        },
        {
            "type": "response_item",
            "timestamp": "2026-11-01T10:00:05Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"text": "<environment_context>x</environment_context>"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-11-01T10:00:09Z",
            "payload": {"type": "message", "role": "assistant", "content": [{"text": "先看设备状态"}]},
        },
        {
            "type": "response_item",
            "timestamp": "2026-11-01T10:00:20Z",
            "payload": {
                "type": "function_call",
                "name": "exec",
                "arguments": "pnputil /enum-devices",
                "call_id": "call_1",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-11-01T10:00:22Z",
            "payload": {"type": "function_call_output", "call_id": "call_1", "output": "code 43"},
        },
    ]
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8"
    )
    return path


class RoundTripTest(unittest.TestCase):
    def test_parse_skips_environment_context(self):
        tmp = _tmpdir()
        thread = codex.parse_file(_rollout(tmp / "rollout-1.jsonl"))
        self.assertEqual(thread.thread_id, "abc-123")
        self.assertEqual(thread.cwd, "C:/work")
        self.assertEqual(thread.counts()["user"], 1)
        self.assertEqual(thread.counts()["tool_call"], 1)
        self.assertEqual(thread.tool_names(), ["exec"])

    def test_bundle_roundtrip_keeps_events(self):
        tmp = _tmpdir()
        thread = codex.parse_file(_rollout(tmp / "rollout-1.jsonl"))
        out = bundle.write(thread, tmp / "handoff", render.l1(thread))
        loaded = bundle.read(out)
        self.assertEqual(loaded["thread"], "abc-123")
        self.assertEqual(len(loaded["_events"]), 6)

    def test_bundle_detects_tampering(self):
        tmp = _tmpdir()
        thread = codex.parse_file(_rollout(tmp / "rollout-1.jsonl"))
        out = bundle.write(thread, tmp / "handoff", render.l1(thread))
        events = out / "events.jsonl"
        events.write_text(events.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            bundle.read(out)

    def test_render_levels_differ(self):
        tmp = _tmpdir()
        thread = codex.parse_file(_rollout(tmp / "rollout-1.jsonl"))
        low, mid, high = render.l0(thread), render.l1(thread), render.l2(thread)
        self.assertLess(len(low), len(mid))
        self.assertLess(len(mid), len(high))
        self.assertIn("帮我修耳机", mid)
        self.assertIn("code 43", high)
        self.assertNotIn("code 43", mid)

    def test_brief_fits_budget(self):
        tmp = _tmpdir()
        thread = codex.parse_file(_rollout(tmp / "rollout-1.jsonl"))
        text = render.brief(thread, budget=600)
        self.assertLessEqual(len(text.encode("utf-8")), 600)
        self.assertIn("## 目标", text)
        self.assertIn("帮我修耳机", text)

    def test_brief_is_smaller_than_outline(self):
        tmp = _tmpdir()
        thread = codex.parse_file(_rollout(tmp / "rollout-1.jsonl"))
        self.assertLess(len(render.brief(thread)), len(render.l1(thread)))

    def test_assess_marks_unanswered_question(self):
        tmp = _tmpdir()
        path = tmp / "rollout-2.jsonl"
        ask = json.dumps({"questions": [{"title": "请拔出耳机再插回", "options": ["好了"]}]}, ensure_ascii=False)
        lines = [
            {"type": "session_meta", "payload": {"id": "t2", "cwd": "C:/w"}},
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:00:00Z",
                "payload": {"type": "message", "role": "user", "content": [{"text": "耳机不识别"}]},
            },
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:00:10Z",
                "payload": {"type": "function_call", "name": "request_user_input_async", "arguments": ask, "call_id": "c1"},
            },
        ]
        path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8")
        state = render.assess(codex.parse_file(path))
        self.assertEqual(state["status"], "in_progress")
        self.assertIn("等待用户回答", state["next_steps"][0])
        note = render.resume(codex.parse_file(path))
        self.assertIn("## 下一步", note)
        self.assertIn("拔出耳机", note)

    def test_assess_does_not_treat_completion_claim_as_proof(self):
        tmp = _tmpdir()
        path = tmp / "rollout-3.jsonl"
        lines = [
            {"type": "session_meta", "payload": {"id": "t3", "cwd": "C:/w"}},
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:00:00Z",
                "payload": {"type": "message", "role": "user", "content": [{"text": "做个 ppt"}]},
            },
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:05:00Z",
                "payload": {"type": "message", "role": "assistant", "content": [{"text": "已完成，文件在 outputs 里。"}]},
            },
        ]
        path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8")
        state = render.assess(codex.parse_file(path))
        self.assertEqual(state["status"], "unknown")
        self.assertIn("尚未核验", state["reason"])


    def test_redaction_masks_secrets(self):
        cleaned, hits = redact.redact_text("key=sk-abcdefghijklmnopqrstuvwx 和 AKIAABCDEFGHIJKLMNOP")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", cleaned)
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", cleaned)
        self.assertIn("[REDACTED", cleaned)
        self.assertTrue(hits)

    def test_redact_thread_cleans_tool_args(self):
        tmp = _tmpdir()
        thread = codex.parse_file(_rollout(tmp / "rollout-1.jsonl"))
        thread.events[2].args = "export TOKEN=sk-abcdefghijklmnopqrstuvwx"
        hits = redact.redact_thread(thread)
        self.assertIn("openai", hits)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", thread.events[2].args)


    def test_claim_is_unverified_without_confirmation(self):
        tmp = _tmpdir()
        path = tmp / "rollout-4.jsonl"
        lines = [
            {"type": "session_meta", "payload": {"id": "t4", "cwd": "C:/w"}},
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:00:00Z",
                "payload": {"type": "message", "role": "user", "content": [{"text": "修一下"}]},
            },
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:01:00Z",
                "payload": {"type": "message", "role": "assistant", "content": [{"text": "已完成，改好了。"}]},
            },
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:02:00Z",
                "payload": {"type": "message", "role": "user", "content": [{"text": "好了"}]},
            },
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:03:00Z",
                "payload": {"type": "message", "role": "assistant", "content": [{"text": "已完成，改好了。"}]},
            },
        ]
        path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8")
        state = render.assess(codex.parse_file(path))
        self.assertTrue(state["claims"])
        self.assertEqual(state["claims"][0]["state"], "unverified")
        self.assertEqual(state["claims"][0]["reaction"]["text"], "好了")
        self.assertEqual(state["claims"][-1]["state"], "unverified")
        self.assertIn("[#", render.resume(codex.parse_file(path)))

    def test_claim_keeps_later_complaint_as_evidence_for_review(self):
        tmp = _tmpdir()
        path = tmp / "rollout-6.jsonl"
        lines = [
            {"type": "session_meta", "payload": {"id": "t6", "cwd": "C:/w"}},
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:00:00Z",
                "payload": {"type": "message", "role": "user", "content": [{"text": "耳机还是断"}]},
            },
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:01:00Z",
                "payload": {"type": "message", "role": "assistant", "content": [{"text": "已修复，问题解决了。"}]},
            },
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:05:00Z",
                "payload": {"type": "message", "role": "user", "content": [{"text": "还是不行，又断了"}]},
            },
        ]
        path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8")
        claims = render.assess(codex.parse_file(path))["claims"]
        self.assertEqual(claims[0]["state"], "unverified")
        self.assertEqual(claims[0]["reaction"]["text"], "还是不行，又断了")
        self.assertIn("待核验", render.resume(codex.parse_file(path)))


    def test_constraints_extract_forbidden_and_required(self):
        tmp = _tmpdir()
        path = tmp / "rollout-5.jsonl"
        lines = [
            {"type": "session_meta", "payload": {"id": "t5", "cwd": "C:/w"}},
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:00:00Z",
                "payload": {"type": "message", "role": "user", "content": [{"text": "做个幻灯片，不要加多余的东西"}]},
            },
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:01:00Z",
                "payload": {"type": "message", "role": "user", "content": [{"text": "能不能再改改"}]},
            },
        ]
        path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8")
        constraints = render.assess(codex.parse_file(path))["constraints"]
        self.assertEqual(len(constraints), 1)
        self.assertEqual(constraints[0]["kind"], "forbid")
        self.assertIn("不要加多余的东西", constraints[0]["text"])
        self.assertIn("硬约束", render.resume(codex.parse_file(path)))


    def test_conflicts_detect_opposite_assertions(self):
        tmp = _tmpdir()
        path = tmp / "rollout-7.jsonl"
        lines = [
            {"type": "session_meta", "payload": {"id": "t7", "cwd": "C:/w"}},
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:00:00Z",
                "payload": {"type": "message", "role": "assistant", "content": [{"text": "建议重启电脑试试。"}]},
            },
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:05:00Z",
                "payload": {"type": "message", "role": "assistant", "content": [{"text": "不建议重启电脑，先查驱动。"}]},
            },
        ]
        path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8")
        found = conflicts.detect(codex.parse_file(path))
        self.assertEqual(len(found), 1)
        self.assertIn("重启电脑", found[0]["topic"])

    def test_snapshot_captures_recent_files(self):
        tmp = _tmpdir()
        (tmp / "note.txt").write_text("hello", encoding="utf-8")
        snap = snapshot.capture(str(tmp))
        paths = [item["path"] for item in snap["recent_files"]]
        self.assertIn("note.txt", paths)


    def test_constraints_ignore_substring_traps(self):
        tmp = _tmpdir()
        path = tmp / "rollout-8.jsonl"
        lines = [
            {"type": "session_meta", "payload": {"id": "t8", "cwd": "C:/w"}},
            {
                "type": "response_item",
                "timestamp": "2026-11-01T10:00:00Z",
                "payload": {"type": "message", "role": "user", "content": [{"text": "识别有问题，连不上耳机"}]},
            },
        ]
        path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8")
        self.assertEqual(render.assess(codex.parse_file(path))["constraints"], [])


    def test_resume_shows_human_state_first(self):
        tmp = _tmpdir()
        thread = codex.parse_file(_rollout(tmp / "rollout-1.jsonl"))
        text = render.resume(thread, state_data={"task": "修耳机", "status": "paused", "next_step": "先拔线", "do_not": ["别重启"]})
        self.assertIn("交接人填写的状态", text)
        self.assertLess(text.index("交接人填写"), text.index("## 当前状态"))
        self.assertIn("别重启", text)


if __name__ == "__main__":
    unittest.main()
