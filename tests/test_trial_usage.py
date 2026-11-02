import contextlib
import io
import json
import sys
from support import WorkspaceTemporaryDirectory
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from baton import measure, trial_usage
from baton.model import Event, Thread

TMP = Path(__file__).resolve().parents[1] / ".tmp"
TMP.mkdir(exist_ok=True)


def usage(inputs=100, outputs=20, cached=60, reasoning=5):
    return {"input_tokens": inputs, "output_tokens": outputs, "total_tokens": inputs + outputs,
            "cached_input_tokens": cached, "reasoning_output_tokens": reasoning, "cache_write_input_tokens": 0}


def event(kind, timestamp="2026-01-01T00:00:00Z", **data):
    return {"type": "event_msg", "timestamp": timestamp, "payload": {"type": kind, **data}}


class TrialUsageTest(unittest.TestCase):
    def inspect(self, rows):
        with WorkspaceTemporaryDirectory(dir=TMP) as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text("\n".join(json.dumps(row) if not isinstance(row, str) else row for row in rows), encoding="utf-8")
            return trial_usage.inspect_rollout(path)

    def test_unique_response_sum_not_cumulative_sum_and_no_subset_double_count(self):
        rows = [event("task_started"),
                {"type": "token_usage_record", "payload": {"response_id": "a", "usage": usage()}},
                event("token_count", info={"total_token_usage": usage()}),
                {"type": "token_usage_record", "payload": {"response_id": "b", "usage": usage()}},
                event("token_count", info={"total_token_usage": usage(200, 40, 120, 10)}),
                event("task_complete", "2026-01-01T00:00:10Z")]
        result = self.inspect(rows)
        self.assertEqual(result["usage"]["total_tokens"], 240)
        self.assertEqual(result["usage"]["cached_input_tokens"], 120)
        self.assertEqual(result["usage"]["reasoning_output_tokens"], 10)
        self.assertTrue(result["measurement_finalized"])
        self.assertEqual(result["elapsed_seconds"], 10)

    def test_duplicate_response_is_counted_once(self):
        row = {"type": "token_usage_record", "payload": {"response_id": "a", "usage": usage()}}
        result = self.inspect([event("task_started"), row, row, event("task_complete")])
        self.assertEqual(result["unique_responses"], 1)
        self.assertEqual(result["usage"]["total_tokens"], 120)

    def test_reported_total_mismatch_is_not_final(self):
        result = self.inspect([event("task_started"),
            {"type": "token_usage_record", "payload": {"response_id": "a", "usage": usage()}},
            event("token_count", info={"total_token_usage": usage(500, 20, 60)}), event("task_complete")])
        self.assertFalse(result["consistent_with_cumulative"])
        self.assertFalse(result["measurement_finalized"])

    def test_old_cumulative_only_format_is_supported_without_summing_snapshots(self):
        result = self.inspect([event("task_started"),
            event("token_count", info={"total_token_usage": usage()}),
            event("token_count", info={"total_token_usage": usage(300, 40, 120)}), event("task_complete")])
        self.assertEqual(result["usage"]["total_tokens"], 340)
        self.assertEqual(result["method"], "last_reported_cumulative_usage")
        self.assertTrue(result["measurement_finalized"])

    def test_running_or_malformed_session_is_not_final(self):
        row = {"type": "token_usage_record", "payload": {"response_id": "a", "usage": usage()}}
        self.assertFalse(self.inspect([event("task_started"), row])["measurement_finalized"])
        result = self.inspect([event("task_started"), row, '{"incomplete":', event("task_complete")])
        self.assertEqual(result["malformed_records"], 1)
        self.assertFalse(result["measurement_finalized"])

    def test_missing_usage_remains_unknown_instead_of_zero(self):
        result = self.inspect([event("task_started"), event("task_complete")])
        self.assertIsNone(result["usage"])
        self.assertFalse(result["measurement_finalized"])

    def test_account_and_message_content_are_not_exported(self):
        result = self.inspect([
            {"type": "session_meta", "payload": {"creator_account_id": "private-account", "agent_path": "/root/test"}},
            {"type": "turn_context", "payload": {"model": "local-model"}},
            {"type": "response_item", "payload": {"type": "message", "content": "private-message"}},
            event("task_started"), event("task_complete")])
        self.assertNotIn("private-account", json.dumps(result))
        self.assertNotIn("private-message", json.dumps(result))
        self.assertEqual(result["agent_path"], "/root/test")
        self.assertEqual(result["models"], ["local-model"])

    def test_invalid_native_record_cannot_be_masked_by_cumulative_fallback(self):
        result = self.inspect([event("task_started"),
            {"type": "token_usage_record", "payload": {"response_id": "a", "usage": {"input_tokens": -1}}},
            event("token_count", info={"total_token_usage": usage()}), event("task_complete")])
        self.assertFalse(result["measurement_finalized"])
        self.assertIsNone(result["usage"])

    def test_optional_metrics_remain_null_when_unreported(self):
        basic = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
        result = self.inspect([event("task_started"), event("token_count", info={"total_token_usage": basic}), event("task_complete")])
        self.assertIsNone(result["usage"]["cached_input_tokens"])
        self.assertIsNone(result["usage"]["reasoning_output_tokens"])

    def test_eval_excludes_both_guardian_and_experiment_subagents(self):
        threads = [Thread("codex", "root", events=[Event(1, "", "user", text="任务")]),
                   Thread("codex", "child", events=[Event(1, "", "user", text="子任务")], meta={"thread_source": "subagent"}),
                   Thread("codex", "review", events=[], meta={"thread_source": "guardian_review"})]
        entries = [{"path": thread.thread_id} for thread in threads]
        output = io.StringIO()
        with patch.object(measure.SOURCES["codex"], "list_sessions", return_value=entries), \
             patch.object(measure.SOURCES["codex"], "parse_file", side_effect=threads), \
             contextlib.redirect_stdout(output):
            self.assertEqual(measure.run("codex"), 0)
        self.assertIn("会话 1 条（跳过子线程 2 条", output.getvalue())


if __name__ == "__main__":
    unittest.main()
