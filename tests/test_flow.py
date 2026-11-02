import contextlib
import io
import json
import sys
from support import WorkspaceTemporaryDirectory
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from baton import bridge, cli, flow
from baton.model import Event, Thread

TMP = Path(__file__).resolve().parents[1] / ".tmp"
TMP.mkdir(exist_ok=True)


def native(host="codex"):
    return Thread(host, host + "-real", cwd="C:/project", events=[Event(1, "", "user", text="继续项目")])


def initial():
    return {"task": "实际接手", "status": "paused", "next_step": "核对项目", "confirmed": [], "evidence": {"reference": "original"}}


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.temporary = WorkspaceTemporaryDirectory(dir=TMP)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sent = bridge.send(self.root, native(), "dsh", initial())

    def take(self):
        return flow.take(self.root, "dsh")

    def test_resume_preserves_edits_and_new_pending_does_not_replace_active(self):
        accepted = self.take()
        draft = Path(accepted["state_path"])
        changed = initial(); changed["next_step"] = "继续尚未完成的修改"
        draft.write_text(json.dumps(changed), encoding="utf-8-sig")
        original = (Path(accepted["packet_path"]) / "STATE.json").read_bytes()
        bridge.send(self.root, native(), "dsh", initial())
        resumed = self.take()
        self.assertEqual(resumed["id"], self.sent["id"])
        self.assertEqual(resumed["state"], changed)
        self.assertEqual((Path(accepted["packet_path"]) / "STATE.json").read_bytes(), original)

    def test_switch_requires_explicit_choice_and_keeps_both_drafts(self):
        first = self.take()
        second = bridge.send(self.root, native(), "dsh", initial())
        with self.assertRaisesRegex(ValueError, "switch"):
            flow.take(self.root, "dsh", second["id"])
        self.assertIsNone(bridge.inspect(self.root, second["id"])["receipt"])
        selected = flow.take(self.root, "dsh", second["id"], switch=True)
        self.assertEqual(selected["id"], second["id"])
        self.assertTrue(Path(first["state_path"]).is_file())
        self.assertTrue(Path(selected["state_path"]).is_file())

    def test_reply_uses_updated_draft_and_is_idempotent(self):
        accepted = self.take()
        changed = initial(); changed["confirmed"] = ["已在本端核对工作区"]
        Path(accepted["state_path"]).write_text(json.dumps(changed), encoding="utf-8")
        sent = flow.reply(self.root, "dsh", native("dsh"))
        self.assertFalse(sent["reused"])
        back = bridge.receive(self.root, "codex", sent["id"])
        self.assertEqual(back["state"], changed)
        self.assertEqual(back["reply_to"], self.sent["id"])
        self.assertEqual(back["chain"], self.sent["id"])
        repeated = flow.reply(self.root, "dsh", native("dsh"))
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["id"], sent["id"])
        self.assertEqual(len(bridge.inbox(self.root, "codex", True)), 1)
        self.assertEqual(flow.current(self.root, "dsh")["phase"], "returned")
        with self.assertRaisesRegex(ValueError, "已经返回"):
            flow.take(self.root, "dsh", self.sent["id"])

    def test_reply_recovers_published_packet_after_pointer_write_failure(self):
        self.take()
        with patch("baton.flow._save", side_effect=OSError("interrupted pointer write")):
            with self.assertRaises(OSError): flow.reply(self.root, "dsh", native("dsh"))
        packets = bridge.inbox(self.root, "codex", True)
        self.assertEqual(len(packets), 1)
        recovered = flow.reply(self.root, "dsh", native("dsh"))
        self.assertEqual(recovered["id"], packets[0]["id"])
        self.assertTrue(recovered["reused"])
        self.assertEqual(flow.current(self.root, "dsh")["phase"], "returned")

    def test_invalid_draft_can_be_found_and_fixed_without_sending(self):
        accepted = self.take(); draft = Path(accepted["state_path"])
        draft.write_text("{broken", encoding="utf-8")
        found = flow.current(self.root, "dsh")
        self.assertEqual(found["state_path"], str(draft))
        self.assertTrue(found["state_problems"])
        with self.assertRaisesRegex(ValueError, "修正"):
            flow.reply(self.root, "dsh", native("dsh"))
        self.assertEqual(bridge.inbox(self.root, "codex", True), [])
        draft.write_text(json.dumps(initial()), encoding="utf-8")
        self.assertEqual(self.take()["state"], initial())

    def test_wrong_host_or_session_never_acknowledges_or_returns(self):
        with self.assertRaises(ValueError): flow.take(self.root, "codex", self.sent["id"])
        self.assertIsNone(bridge.inspect(self.root, self.sent["id"])["receipt"])
        self.take()
        with self.assertRaises(ValueError): flow.reply(self.root, "dsh", native("codex"))
        self.assertEqual(bridge.inbox(self.root, "codex", True), [])

    def test_stale_lock_does_not_acknowledge_or_get_removed(self):
        lock = self.root / "active/dsh.lock"; lock.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "锁目录"): self.take()
        self.assertTrue(lock.is_dir())
        self.assertIsNone(bridge.inspect(self.root, self.sent["id"])["receipt"])

    def test_pointer_cannot_escape_or_ignore_packet_integrity(self):
        self.take()
        pointer = self.root / "active/dsh.json"
        active = json.loads(pointer.read_text(encoding="utf-8"))
        active["transfer"] = "../../outside"
        pointer.write_text(json.dumps(active), encoding="utf-8")
        with self.assertRaises(ValueError): flow.current(self.root, "dsh")
        active["transfer"] = self.sent["id"]; active["seal"] = "wrong"
        pointer.write_text(json.dumps(active), encoding="utf-8")
        with self.assertRaises(ValueError): flow.current(self.root, "dsh")

    def test_current_without_a_task_is_read_only(self):
        untouched = self.root / "not-created"
        self.assertIsNone(flow.current(untouched, "codex"))
        self.assertFalse(untouched.exists())

    def test_two_hosts_can_take_reply_and_continue_the_same_chain(self):
        self.take()
        back = flow.reply(self.root, "dsh", native("dsh"))
        accepted = flow.take(self.root, "codex")
        self.assertEqual(accepted["id"], back["id"])
        next_packet = flow.reply(self.root, "codex", native("codex"))
        resumed = flow.take(self.root, "dsh")
        self.assertEqual(resumed["id"], next_packet["id"])
        self.assertEqual(resumed["chain"], self.sent["id"])
        # After switching to a newer task, an old returned task stays closed.
        with self.assertRaisesRegex(ValueError, "已有返回包"):
            flow.take(self.root, "dsh", self.sent["id"], switch=True)

    def test_cli_take_task_and_reply_need_no_parent_or_state_path(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main(["take", "--host", "dsh", "--root", str(self.root), "--json"])
        self.assertEqual(json.loads(output.getvalue())["id"], self.sent["id"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main(["task", "--host", "dsh", "--root", str(self.root), "--json"])
        self.assertEqual(json.loads(output.getvalue())["phase"], "active")
        output = io.StringIO()
        with patch("baton.sessions.resolve", return_value=native("dsh")), contextlib.redirect_stdout(output):
            cli.main(["reply", "--host", "dsh", "--thread", "dsh-real", "--root", str(self.root), "--json"])
        self.assertEqual(json.loads(output.getvalue())["reply_to"], self.sent["id"])


if __name__ == "__main__":
    unittest.main()
