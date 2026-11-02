"""Contracts that prevent a handoff from dropping evidence or inventing verification."""

import contextlib
import io
import json
import sys
from support import WorkspaceTemporaryDirectory
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from baton import bundle, cli, redact, render, state, trial
from baton.model import Event, Thread
from baton.sources import codex, dsh

TEST_TMP = Path(__file__).resolve().parents[1] / ".tmp"
TEST_TMP.mkdir(exist_ok=True)


def temporary_directory():
    return WorkspaceTemporaryDirectory(dir=TEST_TMP)


class HandoffContractTest(unittest.TestCase):
    def test_dsh_unknown_attachment_and_malformed_records_survive_roundtrip(self):
        records = [
            {"type": "session", "id": "native", "cwd": "C:/project"},
            {"type": "future/event", "time": 1, "data": {"nested": {"unicode": "不能丢"}}},
            {"type": "user/message", "time": 2, "data": {"content": [{"type": "image", "attachment": {"name": "plot.png", "id": "asset-42"}}]}},
            '{"truncated":',
            None,
        ]
        thread = dsh.parse_lines(records, "native.jsonl")
        self.assertEqual(len(thread.events), len(records))
        with temporary_directory() as directory:
            out = bundle.write(thread, Path(directory), render.l1(thread))
            events = bundle.read(out)["_events"]
        by_ref = {event.ref: event.raw for event in events}
        for index, record in enumerate(records[:-1], 1):
            self.assertEqual(by_ref[f"native.jsonl#{index}"], record)
        self.assertEqual(by_ref["native.jsonl#5"], {"unparsed_record": None})
        self.assertEqual([event.seq for event in events], list(range(1, 6)))

    def test_codex_unknown_injected_and_original_line_numbers_survive(self):
        records = [
            '{"type":"session_meta","payload":{"id":"native"}}\n',
            '\n',
            '{"type":"future/event","payload":{"a":[1,2]}}\n',
            '{"type":"response_item","payload":{"type":"message","role":"user","content":[{"text":"<environment_context>context"}]}}\n',
            '{"broken":\n',
        ]
        thread = codex.parse_lines(records, "native.jsonl")
        self.assertEqual(len(thread.events), 4)
        self.assertEqual(thread.counts().get("user", 0), 0)
        self.assertEqual({event.ref for event in thread.events}, {"native.jsonl#1", "native.jsonl#3", "native.jsonl#4", "native.jsonl#5"})
        self.assertTrue(all(event.raw is not None for event in thread.events))

    def test_raw_record_secrets_are_redacted_recursively(self):
        native = {"unknown": [{"token": "private-value", "text": "sk-abcdefghijklmnopqrstuvwx"}]}
        thread = Thread("dsh", "secrets", events=[Event(1, "", "meta", raw=native)])
        redact.redact_thread(thread)
        encoded = json.dumps(thread.events[0].raw)
        self.assertNotIn("private-value", encoded)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", encoded)
        self.assertIn("REDACTED", encoded)

    def test_raw_json_strings_and_credential_aliases_are_redacted(self):
        native = {"data": {"arguments": '{"api_key":"opaque-json-secret"}',
                           "authorization": "opaque-authorization", "client_secret": "opaque-client-secret"}}
        thread = Thread("dsh", "secrets", events=[Event(1, "", "meta", raw=native)])
        redact.redact_thread(thread)
        encoded = json.dumps(thread.events[0].raw)
        for secret in ("opaque-json-secret", "opaque-authorization", "opaque-client-secret"):
            self.assertNotIn(secret, encoded)

    def test_secret_in_tool_name_or_source_reference_is_masked(self):
        secret = "sk-abcdefghijklmnopqrstuvwx"
        thread = Thread("dsh", "secrets", events=[Event(1, "", "tool_call", name=secret, ref=secret)])
        redact.redact_thread(thread)
        self.assertNotIn(secret, json.dumps(thread.events[0].to_dict()))

    def test_pem_redaction_removes_body(self):
        cleaned, _ = redact.redact_text("-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----")
        self.assertNotIn("private-material", cleaned)

    def test_duplicate_user_records_still_preserve_both_originals(self):
        row = {"type": "user/message", "time": 1, "data": {"content": [{"type": "text", "text": "继续"}]}}
        thread = dsh.parse_lines([row, row], "native.jsonl")
        self.assertEqual(len(thread.events), 2)
        self.assertEqual(thread.events[1].raw, row)
        self.assertEqual(thread.counts()["user"], 1)

    def test_trailing_metadata_does_not_override_latest_request(self):
        thread = Thread("dsh", "latest", events=[
            Event(1, "", "assistant", text="已完成。"),
            Event(2, "", "user", text="改成按季度统计"),
            Event(3, "", "meta", raw={"type": "turn/end"}),
        ])
        assessed = render.assess(thread)
        self.assertEqual(assessed["status"], "in_progress")
        self.assertEqual(assessed["evidence"], [2])
        self.assertIn("季度", assessed["next_steps"][0])

    def test_thanks_or_unrelated_success_never_verifies_claims(self):
        for reaction in ("thanks", "perfect", "还没好了", "另一台电脑成功了，这台不行", "可以，继续"):
            with self.subTest(reaction=reaction):
                thread = Thread("codex", "claims", events=[
                    Event(1, "", "assistant", text="已修复，fixed."),
                    Event(2, "", "user", text=reaction),
                ])
                assessed = render.assess(thread)
                self.assertTrue(assessed["claims"])
                self.assertTrue(all(claim["state"] == "unverified" for claim in assessed["claims"]))
                self.assertEqual(state.skeleton(thread, assessed)["confirmed"], [])

    def test_later_topic_does_not_get_attached_as_confirmation(self):
        thread = Thread("codex", "topics", events=[
            Event(1, "", "assistant", text="已修复 A。"),
            Event(2, "", "user", text="现在讨论 B"),
            Event(3, "", "assistant", text="已完成 B。"),
            Event(4, "", "user", text="B 可以了"),
        ])
        claims = render.assess(thread)["claims"]
        self.assertEqual(claims[0]["reaction"]["seq"], 2)
        self.assertTrue(all(claim["state"] == "unverified" for claim in claims))

    def test_empty_and_only_unknown_records_produce_readable_resume(self):
        for events in ([], [Event(1, "", "meta", raw={"type": "unknown"})]):
            thread = Thread("dsh", "empty", events=events)
            self.assertEqual(render.assess(thread)["status"], "unknown")
            self.assertIn("没有可识别", render.resume(thread))

    def test_budget_keeps_state_latest_request_and_next_step_before_old_history(self):
        events = [Event(1, "", "user", text="最初目标")]
        events += [Event(i, "", "user", text="早期讨论" * 100) for i in range(2, 60)]
        events.append(Event(60, "", "user", text="最新目标：只导出季度结果"))
        thread = Thread("codex", "long", events=events)
        manual = {"task": "季度结果", "status": "paused", "next_step": "读取新文件", "do_not": ["不要联网"],
                  "confirmed": ["旧文件不完整 [#1]"], "tried": ["旧接口失败"], "open_questions": ["新文件是否齐全"],
                  "evidence": {"source": "new.csv"}}
        note = render.resume(thread, budget=3200, state_data=manual)
        for required in ("只导出季度结果", "读取新文件", "不要联网", "旧文件不完整", "旧接口失败", "新文件是否齐全", "new.csv", "## 当前状态"):
            self.assertIn(required, note)
        self.assertLessEqual(len(note.encode("utf-8")), 3200)

    def test_byte_budget_holds_for_small_and_multibyte_budgets(self):
        for budget in (0, 1, 10, 24, 30, 99, 600):
            self.assertLessEqual(len(render._fit_bytes("中文内容" * 1000, budget).encode("utf-8")), budget)

    def test_latest_assistant_example_is_not_buried_by_old_user_history(self):
        events = [Event(index, "", "user", text="旧讨论" * 100) for index in range(1, 90)]
        events += [Event(90, "", "assistant", text="本次算例：4.0 * 0.5 + 0.0 = 2.0，预测约为0.964；参数没有被训练。"),
                   Event(91, "", "user", text="继续，并沿用刚才的讲法")]
        note = render.resume(Thread("codex", "long", events=events))
        self.assertIn("0.964", note)
        self.assertIn("[#90]", note)

    def test_runtime_metadata_is_not_reported_as_context_compaction(self):
        thread = Thread("codex", "meta", events=[Event(1, "", "user", text="任务"),
            Event(2, "", "meta", text="原始记录：event_msg", raw={"type": "event_msg"}),
            Event(3, "", "meta", text="原始记录：turn_context", raw={"type": "turn_context"})])
        self.assertNotIn("压缩 2 次", render.brief(thread))

    def test_similarity_signatures_are_computed_once_per_user_message(self):
        thread = Thread("codex", "long", events=[Event(index, "", "user", text=f"第{index}次讨论另一个具体事项") for index in range(1,30)])
        with patch.object(render,"_bigrams",wraps=render._bigrams) as signatures:
            render._recurring(thread)
        self.assertEqual(signatures.call_count,29)

    def test_short_message_only_thread_is_shown_in_full_when_it_fits(self):
        thread = Thread("codex", "short", events=[Event(1,"","user",text="想参与项目"),
            Event(2,"","assistant",text="可以整理文档或修代码"),Event(3,"","user",text="还没选方向")])
        note=render.resume(thread)
        self.assertIn("消息全文",note)
        for event in thread.events:
            self.assertIn(event.text,note)
            self.assertIn(f"[#{event.seq}]",note)

    def test_unknown_structured_evidence_does_not_trigger_short_message_route(self):
        thread=Thread("dsh","short",events=[Event(1,"","user",text="整理数据"),
            Event(2,"","meta",text="新记录",raw={"type":"dataset/reference","data":{"path":"file.csv"}})])
        self.assertNotIn("## 消息全文",render.resume(thread))

    def test_native_runtime_envelopes_do_not_hide_short_message_route(self):
        thread=Thread("codex","native",events=[
            Event(1,"","meta",raw={"type":"world_state","payload":{}}),
            Event(2,"","meta",raw={"type":"response_item","payload":{"type":"message","role":"developer","content":[{"text":"injected-context"}]}}),
            Event(3,"","user",text="我想贡献代码"), Event(4,"","assistant",text="先选一个感兴趣的方向")])
        note=render.resume(thread)
        self.assertIn("## 消息全文",note)
        self.assertNotIn("injected-context",note)

    def test_compaction_count_uses_native_event_types(self):
        thread=Thread("codex","native",events=[Event(1,"","user",text="任务"),
            Event(2,"","meta",text="原始记录：图片压缩设置",raw={"type":"image_compression_settings"}),
            Event(3,"","meta",text="native marker",raw={"type":"compacted"}),
            Event(4,"","meta",text="native marker",raw={"type":"response_item","payload":{"type":"compaction"}})])
        self.assertIn("压缩 2 次",render.brief(thread))

    def test_read_can_retrieve_tail_and_native_attachment_without_truncation(self):
        thread = Thread("codex", "read", events=[Event(1, "", "tool_result", result="a" * 2000 + "TAIL", raw={"attachment": {"id": "asset-42"}})])
        with temporary_directory() as directory:
            bundle.write(thread, Path(directory), render.l1(thread))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cli.main(["read", directory, "1", "--context", "0"])
            self.assertIn("TAIL", output.getvalue())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cli.main(["read", directory, "1", "--context", "0", "--raw"])
            self.assertIn("asset-42", output.getvalue())

    def test_search_finds_unknown_native_content_and_points_to_seq(self):
        thread = Thread("dsh", "search", events=[Event(1, "", "meta", raw={"future": "Important Needle"})])
        with temporary_directory() as directory:
            bundle.write(thread, Path(directory), render.l1(thread))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cli.main(["search", directory, "needle"])
            self.assertIn("[#1]", output.getvalue())
            self.assertIn("命中 1 条", output.getvalue())

    def test_state_check_refreshes_resume_after_manual_edits(self):
        thread = Thread("codex", "refresh", events=[Event(1, "", "user", text="任务")])
        with temporary_directory() as directory:
            root = Path(directory)
            bundle.write(thread, root, render.l1(thread), env_text="快照证据")
            state.write(root / "STATE.json", {"task": "新任务", "status": "paused", "next_step": "新下一步", "confirmed": ["核对证据"], "do_not": []})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["state", directory, "--check"]), 0)
            note = (root / "RESUME.md").read_text(encoding="utf-8")
            self.assertIn("新下一步", note)
            self.assertIn("核对证据", note)
            self.assertIn("快照证据", note)

    def test_state_check_rejects_wrong_types_without_crashing(self):
        with temporary_directory() as directory:
            path = Path(directory) / "STATE.json"
            for bad in ([], {"task": 42, "status": "paused", "next_step": "do", "do_not": "wrong"}):
                state.write(path, bad)
                self.assertTrue(state.check(path))

    def test_old_format_bundle_still_reads(self):
        thread = Thread("codex", "legacy", events=[Event(1, "", "user", text="旧包")])
        with temporary_directory() as directory:
            root = bundle.write(thread, Path(directory), render.l1(thread))
            path = root / "MANIFEST.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["baton"] = 1
            manifest.pop("metadata")
            path.write_text(json.dumps(manifest), encoding="utf-8")
            loaded = bundle.read(root)
            self.assertEqual(loaded["_events"][0].text, "旧包")
            self.assertIsNone(loaded["_events"][0].raw)

    def test_reexport_without_snapshot_does_not_keep_stale_environment(self):
        thread = Thread("codex", "same", events=[])
        with temporary_directory() as directory:
            root = bundle.write(thread, Path(directory), render.l1(thread), env_text="旧快照")
            bundle.write(thread, root, render.l1(thread))
            self.assertEqual(bundle.read(root)["_env"], "")

    def test_skeleton_constraints_have_source_coordinates(self):
        thread = Thread("codex", "constraints", events=[Event(1, "", "user", text="不要联网")])
        data = state.skeleton(thread, render.assess(thread))
        self.assertIn("[#1]", data["do_not"][0])

    def test_export_does_not_reuse_state_when_seq_mapping_changes(self):
        old = Thread("codex", "same", events=[Event(1, "", "user", text="旧任务", ref="source#2")])
        new = Thread("codex", "same", events=[Event(1, "", "meta", ref="source#1"), Event(2, "", "user", text="旧任务", ref="source#2")])
        with temporary_directory() as directory:
            root = bundle.write(old, Path(directory), render.l1(old))
            state.write(root / "STATE.json", {"task": "旧任务", "status": "paused", "next_step": "见 [#1]"})
            original = (root / "events.jsonl").read_bytes()
            with patch("baton.cli._load", return_value=new), self.assertRaises(SystemExit):
                cli.main(["export", "same", "--out", directory, "--no-snapshot"])
            self.assertEqual((root / "events.jsonl").read_bytes(), original)

    def test_export_rejects_another_thread_in_same_directory(self):
        old = Thread("dsh", "first", events=[])
        new = Thread("dsh", "second", events=[])
        with temporary_directory() as directory:
            root = bundle.write(old, Path(directory), render.l1(old))
            with self.assertRaises(SystemExit):
                cli._check_destination(new, root)

    def test_export_can_append_without_remapping_existing_state_references(self):
        old = Thread("codex", "same", events=[Event(1, "", "user", text="任务", ref="source#1")])
        new = Thread("codex", "same", events=old.events + [Event(2, "", "user", text="新要求", ref="source#2")])
        with temporary_directory() as directory:
            root = bundle.write(old, Path(directory), render.l1(old))
            state.write(root / "STATE.json", {"task": "任务", "status": "paused", "next_step": "保留证据 [#1]"})
            output = io.StringIO()
            with patch("baton.cli._load", return_value=new), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["export", "same", "--out", directory, "--no-snapshot"]), 0)
            self.assertIn("旧 STATE", output.getvalue())
            self.assertIn("保留证据 [#1]", (root / "RESUME.md").read_text(encoding="utf-8"))

    def test_known_message_with_changed_content_shapes_keeps_raw(self):
        dsh_record = {"type": "user/message", "data": {"content": [{"type": "text", "text": 42}, {"type": "image", "attachment": "future-shape"}]}}
        codex_record = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": None}]}}
        self.assertEqual(dsh.parse_lines([dsh_record], "dsh").events[0].raw, dsh_record)
        self.assertEqual(codex.parse_lines([codex_record], "codex").events[0].raw, codex_record)

    def test_trial_keeps_rubric_out_of_receiver_packets(self):
        with temporary_directory() as directory:
            root = Path(directory) / "trial"
            judgements = trial.prepare(root)
            self.assertEqual(judgements.parent.name, "judge")
            self.assertEqual(len(list((root / "receiver").iterdir())), 4)
            self.assertFalse(list((root / "receiver").rglob("judgements.json")))
            for index, case in enumerate(trial.CASES, 1):
                loaded = bundle.read(root / "receiver" / f"case-{index:03}")
                self.assertTrue(loaded["_events"])
                for path in (root / "receiver").rglob("*"):
                    if path.is_file():
                        self.assertNotIn(case["id"], path.read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                trial.prepare(root)

    def test_unrun_trial_never_reports_success_or_zero_tokens(self):
        with temporary_directory() as directory:
            judgements = trial.prepare(Path(directory) / "trial")
            result = trial.score(judgements)
            self.assertEqual(result["passed"], 0)
            self.assertEqual(result["unreviewed"], 12)
            self.assertIsNone(result["pass_rate_on_reviewed"])
            self.assertIsNone(result["full_chain_tokens"])
            self.assertFalse(result["review_complete"])

    def test_trial_requires_answer_and_review_evidence(self):
        with temporary_directory() as directory:
            judgements = trial.prepare(Path(directory) / "trial")
            data = json.loads(judgements.read_text(encoding="utf-8"))
            data["cases"][0]["criteria"][0]["passed"] = True
            judgements.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                trial.score(judgements)
            data["cases"][0]["answer"] = "测试仍失败，不发布"
            data["cases"][0]["criteria"][0]["evidence"] = "回答明确引用非零退出码"
            judgements.write_text(json.dumps(data), encoding="utf-8")
            result = trial.score(judgements)
            self.assertEqual(result["passed"], 1)
            self.assertEqual(result["unreviewed"], 11)
            self.assertFalse(result["review_complete"])

    def test_trial_rejects_missing_case_and_modified_rubric(self):
        with temporary_directory() as directory:
            judgements = trial.prepare(Path(directory) / "trial")
            data = json.loads(judgements.read_text(encoding="utf-8"))
            original = json.loads(json.dumps(data))
            data["cases"].pop()
            judgements.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                trial.score(judgements)
            original["cases"][0]["criteria"][0]["criterion"] = "改掉标准"
            judgements.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaises(ValueError):
                trial.score(judgements)


if __name__ == "__main__":
    unittest.main()
