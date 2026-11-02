import contextlib
import copy
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from support import WorkspaceTemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from baton import bridge, cli, flow, focus, state, workspace
from baton.model import Event, Thread

TMP = Path(__file__).resolve().parents[1] / ".tmp"
TMP.mkdir(exist_ok=True)


def data():
    return {"task": "修当前工程", "status": "paused", "next_step": "检查失败用例", "confirmed": [], "evidence": {}}


def native(host="codex", name=None):
    return Thread(host, name or host + "-user", events=[
        Event(1, "", "user", text="旧任务：不要沿用这条要求"),
        Event(2, "", "tool_call", name="shell", args="current test", call_id="pair"),
        Event(3, "", "tool_result", result="actual failure", call_id="pair"),
        Event(4, "", "user", text="继续当前工程")])


def git_value(_project, *args):
    if args[0] == "rev-parse": return "commit-a\n", ""
    if args[0] == "symbolic-ref": return "main\n", ""
    if args[0] == "status": return " M app.py\0", ""
    raise AssertionError(args)


class ContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemporaryDirectory(dir=TMP)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"; self.project.mkdir()
        (self.project / "app.py").write_text("before", encoding="utf-8")

    def test_content_change_is_detected_when_git_status_does_not_change(self):
        with patch("baton.workspace._git", side_effect=git_value):
            saved = workspace.capture(self.project, ["app.py"])
            self.assertEqual(workspace.compare(saved)["status"], "matched")
            (self.project / "app.py").write_text("after", encoding="utf-8")
            actual = workspace.compare(saved)
            self.assertEqual(actual["status"], "changed")
            self.assertEqual(actual["changes"], [{"kind": "file", "path": "app.py", "before": "hashed", "after": "hashed"}])

    def test_git_version_or_status_changes_are_detected(self):
        with patch("baton.workspace._git", side_effect=git_value): saved = workspace.capture(self.project)
        def changed(project, *args):
            if args[0] == "rev-parse": return "commit-b\n", ""
            if args[0] == "status": return "?? app.py\0", ""
            return git_value(project, *args)
        with patch("baton.workspace._git", side_effect=changed): actual = workspace.compare(saved)
        self.assertEqual(actual["status"], "changed")
        self.assertEqual({item["field"] for item in actual["changes"] if item["kind"] == "git"}, {"head", "status_sha256"})

    def test_explicit_project_mapping_and_missing_directory(self):
        with patch("baton.workspace._git", side_effect=git_value):
            saved = workspace.capture(self.project)
            saved["project"] = str(self.root / "other-machine")
            self.assertEqual(workspace.compare(saved)["status"], "missing")
            actual = workspace.compare(saved, self.project)
            self.assertTrue(actual["mapped"])
            self.assertEqual(actual["status"], "matched")

    def test_omissions_and_unavailable_git_are_reported_as_partial(self):
        with patch("baton.workspace._git", side_effect=git_value), patch("baton.workspace.MAX_FILES", 1):
            saved = workspace.capture(self.project, ["app.py", "missing.py"])
            self.assertEqual(saved["omitted_files"], 1)
            self.assertEqual(workspace.compare(saved)["status"], "partial")
        with patch("baton.workspace._git", return_value=("", "git unavailable")):
            saved = workspace.capture(self.project, ["app.py"])
            self.assertEqual(workspace.compare(saved)["status"], "partial")

    def test_bounds_sensitive_files_and_test_log_provenance(self):
        (self.project / ".env").write_text("secret value", encoding="utf-8")
        (self.project / "test.log").write_text("FAILED", encoding="utf-8")
        with patch("baton.workspace._git", side_effect=git_value):
            saved = workspace.capture(self.project, [".env"], [{"command": "unit tests", "exit_code": 1, "log": "test.log"}])
            self.assertEqual(saved["files"][0]["status"], "excluded_sensitive")
            self.assertEqual(saved["checks"][0]["verification"], "sender_reported")
            self.assertNotIn("secret value", json.dumps(saved))
            (self.project / "test.log").write_text("OK", encoding="utf-8")
            self.assertIn({"kind": "test_log", "path": "test.log"}, workspace.compare(saved)["changes"])
            with self.assertRaises(ValueError): workspace.capture(self.project, ["../outside"])
            with self.assertRaises(ValueError): workspace.capture(self.project, [], [{"command": "tests", "log": "../outside"}])

    def test_explicit_focus_keeps_call_result_and_full_archive(self):
        task = data(); task["context"] = {"refs": [2]}
        thread = native(); original = copy.deepcopy(thread)
        sent = bridge.send(self.root / "mail", thread, "dsh", task)
        checked = bridge.inspect(self.root / "mail", sent["id"])
        self.assertIn("actual failure", checked["focus"])
        self.assertIn("[#2]", checked["focus"]); self.assertIn("[#3]", checked["focus"])
        self.assertNotIn("旧任务", checked["focus"])
        self.assertEqual(len(checked["manifest"]["_events"]), 4)
        self.assertEqual(thread, original)
        self.assertNotIn("origin", task["context"])

    def test_invalid_scope_or_missing_refs_never_publishes(self):
        for context in ({"refs": [999]}, {"since": 999}, {"refs": [2], "origin": {"host": "dsh", "thread": "other"}}, {"files": ["../other"]}):
            task = data(); task["context"] = context
            with self.assertRaises(ValueError): bridge.send(self.root / "mail", native(), "dsh", task)
        items = self.root / "mail/items"
        self.assertFalse(items.exists() and any(items.iterdir()))

    def test_focus_budget_is_measured_in_utf8_bytes(self):
        task = data(); task["next_step"] = "中文" * 300
        text = focus.build(native(), task, budget=350)
        self.assertLessEqual(len(text.encode("utf-8")), 350)
        self.assertIn("超预算", text)

    def test_take_checks_freshness_and_uses_current_draft(self):
        task = data(); task["project"] = str(self.project); task["context"] = {"files": ["app.py"]}
        with patch("baton.workspace._git", side_effect=git_value):
            sent = bridge.send(self.root / "mail", native(), "dsh", task)
            accepted = flow.take(self.root / "mail", "dsh", sent["id"], thread=native("dsh"))
            self.assertEqual(accepted["freshness"]["status"], "matched")
            draft = Path(accepted["state_path"]); changed = json.loads(draft.read_text(encoding="utf-8"))
            changed["next_step"] = "新的实际下一步"; draft.write_text(json.dumps(changed), encoding="utf-8")
            (self.project / "app.py").write_text("modified", encoding="utf-8")
            resumed = flow.take(self.root / "mail", "dsh")
            self.assertEqual(resumed["freshness"]["status"], "changed")
            self.assertIn("新的实际下一步", resumed["focus"])

    def test_new_materials_are_sealed_and_old_packets_are_still_readable(self):
        with patch("baton.workspace._git", side_effect=git_value):
            sent = bridge.send(self.root / "mail", native(), "dsh", data(), project=self.project)
        packet = Path(sent["path"]) / "packet"; old = (packet / "PROJECT.json").read_bytes()
        (packet / "PROJECT.json").write_bytes(old + b" ")
        with self.assertRaisesRegex(ValueError, "已改变"): bridge.receive(self.root / "mail", "dsh", sent["id"])
        (packet / "PROJECT.json").write_bytes(old)
        # Represent a legacy transfer that predates optional project/focus materials.
        path = Path(sent["path"]) / "TRANSFER.json"; transfer = json.loads(path.read_text(encoding="utf-8"))
        for name in ("PROJECT.json", "FOCUS.md"):
            del transfer["files"][name]; (packet / name).unlink()
        transfer.pop("seal"); transfer["seal"] = bridge._digest(transfer)
        path.write_text(json.dumps(transfer), encoding="utf-8")
        accepted = bridge.receive(self.root / "mail", "dsh", sent["id"])
        self.assertIsNone(accepted["project"]); self.assertEqual(accepted["focus"], "")

    def test_binding_rejects_wrong_session_and_can_be_explicitly_rebound(self):
        sent = bridge.send(self.root / "mail", native(), "dsh", data())
        flow.take(self.root / "mail", "dsh", sent["id"], thread=native("dsh", "first"))
        with self.assertRaisesRegex(ValueError, "另一会话"):
            flow.reply(self.root / "mail", "dsh", native("dsh", "wrong"))
        with self.assertRaisesRegex(ValueError, "另一会话"):
            flow.take(self.root / "mail", "dsh", sent["id"], thread=native("dsh", "second"))
        flow.take(self.root / "mail", "dsh", sent["id"], switch=True, thread=native("dsh", "second"))
        self.assertEqual(flow.bound_thread(self.root / "mail", "dsh"), "second")

    def test_cross_host_reply_preserves_old_refs_without_misusing_seq(self):
        task = data(); task["context"] = {"refs": [2]}
        sent = bridge.send(self.root / "mail", native(), "dsh", task)
        flow.take(self.root / "mail", "dsh", sent["id"])
        back = flow.reply(self.root / "mail", "dsh", native("dsh"))
        accepted = bridge.receive(self.root / "mail", "codex", back["id"])
        self.assertNotIn("refs", accepted["state"]["context"])
        previous = accepted["state"]["evidence"]["previous_context"][0]
        self.assertEqual(previous["packet"], sent["id"])
        self.assertEqual(previous["origin"], {"host": "codex", "thread": "codex-user"})

    def test_completed_task_does_not_block_new_pending_task(self):
        sent = bridge.send(self.root / "mail", native(), "dsh", data())
        accepted = flow.take(self.root / "mail", "dsh", sent["id"])
        draft = Path(accepted["state_path"]); done = data(); done["status"] = "done"
        draft.write_text(json.dumps(done), encoding="utf-8")
        self.assertEqual(flow.take(self.root / "mail", "dsh")["state"]["status"], "done")
        new = bridge.send(self.root / "mail", native(), "dsh", data())
        self.assertEqual(flow.take(self.root / "mail", "dsh")["id"], new["id"])
        self.assertEqual(json.loads(draft.read_text(encoding="utf-8"))["status"], "done")

    def test_cli_reply_without_id_uses_bound_native_session(self):
        sent = bridge.send(self.root / "mail", native(), "dsh", data())
        with patch("baton.sessions.resolve", return_value=native("dsh")), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["take", sent["id"], "--host", "dsh", "--thread", "dsh-user", "--root", str(self.root / "mail")])
        with patch("baton.sessions.resolve", return_value=native("dsh")) as resolve, contextlib.redirect_stdout(io.StringIO()):
            cli.main(["reply", "--host", "dsh", "--root", str(self.root / "mail")])
        resolve.assert_called_once_with("dsh", "dsh-user")

    def test_mapped_project_is_used_when_returning(self):
        task = data(); task["project"] = str(self.project)
        other = self.root / "mapped"; other.mkdir()
        (other / "app.py").write_text("before", encoding="utf-8")
        with patch("baton.workspace._git", side_effect=git_value):
            sent = bridge.send(self.root / "mail", native(), "dsh", task)
            accepted = flow.take(self.root / "mail", "dsh", sent["id"], project=other)
            self.assertTrue(accepted["freshness"]["mapped"])
            back = flow.reply(self.root / "mail", "dsh", native("dsh"))
        checked = bridge.inspect(self.root / "mail", back["id"])
        self.assertEqual(checked["project"]["project"], str(other.resolve()))
        self.assertEqual(json.loads(checked["manifest"]["_state"])["project"], str(other.resolve()))

    def test_new_materials_redact_secrets_in_reported_commands(self):
        task = data(); task["checks"] = [{"command": "tests token=opaque-fixture-secret", "exit_code": 1}]
        with patch("baton.workspace._git", side_effect=git_value):
            sent = bridge.send(self.root / "mail", native(), "dsh", task, project=self.project)
        checked = bridge.inspect(self.root / "mail", sent["id"])
        encoded = json.dumps({"project": checked["project"], "focus": checked["focus"], "state": checked["manifest"]["_state"]})
        self.assertNotIn("opaque-fixture-secret", encoded)

    def test_unbound_reply_does_not_guess_latest_session(self):
        sent = bridge.send(self.root / "mail", native(), "dsh", data())
        flow.take(self.root / "mail", "dsh", sent["id"])
        with patch("baton.sessions.resolve") as resolve:
            with self.assertRaisesRegex(SystemExit, "尚未绑定"):
                cli.main(["reply", "--host", "dsh", "--root", str(self.root / "mail")])
            resolve.assert_not_called()

    def test_current_evidence_uses_bound_receiver_and_labels_live_seq(self):
        sent = bridge.send(self.root / "mail", native(), "dsh", data())
        receiver = native("dsh")
        receiver.events[2].result = "token=opaque-live-secret"
        accepted = flow.take(self.root / "mail", "dsh", sent["id"], thread=receiver)
        draft = Path(accepted["state_path"]); updated = data()
        updated["context"] = {"refs": [3], "origin": {"host": "dsh", "thread": receiver.thread_id}}
        draft.write_text(json.dumps(updated), encoding="utf-8")
        with patch("baton.sessions.resolve", return_value=receiver) as resolve:
            actual = flow.current(self.root / "mail", "dsh")
        resolve.assert_called_once_with("dsh", receiver.thread_id)
        self.assertEqual(actual["state_problems"], [])
        self.assertIn("实时记录（尚未封存）", actual["focus"])
        self.assertIn("不能套到接收的旧包", actual["focus"])
        self.assertNotIn("opaque-live-secret", actual["focus"])
        self.assertIn("opaque-live-secret", receiver.events[2].result)


if __name__ == "__main__":
    unittest.main()
