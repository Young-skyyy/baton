import contextlib
import io
import json
import sys
from support import WorkspaceTemporaryDirectory
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from baton import bridge, bundle, cli, sessions
from baton.model import Event, Thread
from baton.sources import codex, dsh

TMP = Path(__file__).resolve().parents[1] / ".tmp"
TMP.mkdir(exist_ok=True)


def task(host="codex", identifier="thread-1"):
    return Thread(host, identifier, cwd="C:/project", events=[Event(1, "", "user", text="先打通 DSH 与 Codex")])


def state():
    return {"task": "双向接手", "status": "paused", "next_step": "接收并返回", "confirmed": [],
            "do_not": ["不执行历史命令"], "evidence": {"nonce": "test-roundtrip"}}


class BridgeTest(unittest.TestCase):
    def test_codex_to_dsh_and_back_forms_a_receipted_chain(self):
        with WorkspaceTemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            sent = bridge.send(root, task(), "dsh", state())
            self.assertEqual(bridge.inbox(root, "dsh")[0]["id"], sent["id"])
            accepted = bridge.receive(root, "dsh", sent["id"])
            self.assertEqual(accepted["state"]["evidence"]["nonce"], "test-roundtrip")
            self.assertEqual(bridge.inbox(root, "dsh"), [])
            back = bridge.send(root, task("dsh", "dsh-native"), "codex", state(), reply_to=sent["id"])
            received = bridge.receive(root, "codex", back["id"])
            self.assertEqual(received["chain"], sent["id"])
            self.assertEqual(received["reply_to"], sent["id"])
            self.assertTrue((root / "items" / sent["id"] / "RECEIPT.json").exists())
            self.assertTrue((root / "items" / back["id"] / "RECEIPT.json").exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows directory access regression")
    def test_packet_directory_inherits_shared_workspace_access(self):
        import ctypes
        from ctypes import wintypes
        with WorkspaceTemporaryDirectory(dir=TMP) as directory:
            sent = bridge.send(Path(directory), task(), "dsh", state())
            for path in (Path(directory), Path(sent["path"]), Path(sent["path"]) / "packet"):
                needed = wintypes.DWORD()
                api = ctypes.WinDLL("advapi32", use_last_error=True)
                api.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(needed))
                self.assertGreater(needed.value, 0)
                descriptor = ctypes.create_string_buffer(needed.value)
                self.assertTrue(api.GetFileSecurityW(str(path), 4, descriptor, needed.value, ctypes.byref(needed)))
                control = wintypes.WORD(); revision = wintypes.DWORD()
                self.assertTrue(api.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)))
                self.assertFalse(control.value & 0x1000, "Published shared directory must inherit access")

    def test_wrong_host_cannot_receive_or_create_receipt(self):
        with WorkspaceTemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            sent = bridge.send(root, task(), "dsh", state())
            with self.assertRaises(ValueError): bridge.receive(root, "codex", sent["id"])
            self.assertFalse((root / "items" / sent["id"] / "RECEIPT.json").exists())

    def test_receiving_by_id_is_idempotent_and_preserves_first_receipt(self):
        with WorkspaceTemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            sent = bridge.send(root, task(), "dsh", state())
            first = bridge.receive(root, "dsh", sent["id"], note="first")
            second = bridge.receive(root, "dsh", sent["id"], note="second")
            self.assertEqual(first["receipt"], second["receipt"])

    def test_latest_selects_only_pending_for_the_receiver(self):
        with WorkspaceTemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            first = bridge.send(root, task(), "dsh", state())
            second = bridge.send(root, task(identifier="second"), "dsh", state())
            self.assertEqual(bridge.receive(root, "dsh")["id"], second["id"])
            self.assertEqual(bridge.receive(root, "dsh")["id"], first["id"])
            with self.assertRaises(ValueError): bridge.receive(root, "dsh")

    def test_sealed_state_and_resume_cannot_silently_change(self):
        for filename in ("STATE.json", "RESUME.md", "events.jsonl"):
            with WorkspaceTemporaryDirectory(dir=TMP) as directory:
                root = Path(directory)
                sent = bridge.send(root, task(), "dsh", state())
                file = root / "items" / sent["id"] / "packet" / filename
                file.write_bytes(file.read_bytes() + b"\n")
                with self.assertRaises(ValueError): bridge.receive(root, "dsh", sent["id"])

    def test_transfer_metadata_tampering_is_detected(self):
        with WorkspaceTemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            sent = bridge.send(root, task(), "dsh", state())
            path = root / "items" / sent["id"] / "TRANSFER.json"
            data = json.loads(path.read_text(encoding="utf-8")); data["to"] = "codex"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError): bridge.inspect(root, sent["id"])

    def test_return_requires_receipt_and_the_reverse_direction(self):
        with WorkspaceTemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            sent = bridge.send(root, task(), "dsh", state())
            with self.assertRaises(ValueError): bridge.send(root, task("dsh"), "codex", state(), reply_to=sent["id"])
            bridge.receive(root, "dsh", sent["id"])
            with self.assertRaises(ValueError): bridge.send(root, task(), "dsh", state(), reply_to=sent["id"])

    def test_invalid_state_does_not_publish_partial_transfer(self):
        with WorkspaceTemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            with self.assertRaises(ValueError): bridge.send(root, task(), "dsh", {"task": "missing fields"})
            items = root / "items"
            self.assertFalse(items.exists() and any(items.iterdir()))

    def test_manual_state_note_and_source_are_redacted_without_mutation(self):
        native = task()
        native.events[0].text = "sk-abcdefghijklmnopqrstuvwx"
        original = state(); original["evidence"]["api_key"] = "opaque-manual-secret"
        with WorkspaceTemporaryDirectory(dir=TMP) as directory:
            sent = bridge.send(Path(directory), native, "dsh", original, note="token=opaque-note-secret")
            received = bridge.receive(Path(directory), "dsh", sent["id"])
            encoded = json.dumps(received)
            self.assertNotIn("opaque-manual-secret", encoded)
            self.assertNotIn("opaque-note-secret", encoded)
            self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", encoded)
        self.assertEqual(original["evidence"]["api_key"], "opaque-manual-secret")
        self.assertEqual(native.events[0].text, "sk-abcdefghijklmnopqrstuvwx")

    def test_transfer_id_cannot_escape_root(self):
        for identifier in ("../outside", "C:/outside", "handoff-" + "a" * 20 + "/../"):
            with self.assertRaises(ValueError): bridge.receive(TMP, "dsh", identifier)

    def test_subagent_cannot_be_exported_as_a_user_task(self):
        native = task(); native.meta["thread_source"] = "subagent"
        with self.assertRaises(ValueError): bridge.send(TMP, native, "dsh", state())

    def test_powershell_utf8_bom_state_can_be_handed_off_and_read(self):
        with WorkspaceTemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            state_path = root / "state.json"
            state_path.write_text(json.dumps(state(), ensure_ascii=False), encoding="utf-8-sig")
            output = io.StringIO()
            with patch("baton.sessions.resolve", return_value=task()), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["handoff", "--from", "codex", "--to", "dsh", "--root", directory, "--state", str(state_path), "--json"]), 0)
            identifier = json.loads(output.getvalue())["id"]
            accepted = bridge.receive(root, "dsh", identifier)
            self.assertEqual(accepted["state"], state())
            # An older standalone bundle may have been edited with Windows PowerShell.
            packet = Path(accepted["path"])
            (packet / "STATE.json").write_text(json.dumps(state()), encoding="utf-8-sig")
            self.assertEqual(json.loads(bundle.read(packet)["_state"]), state())

    def test_cli_send_list_receive_and_reply(self):
        with WorkspaceTemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            state_path = root / "state.json"; state_path.write_text(json.dumps(state()), encoding="utf-8")
            output = io.StringIO()
            with patch("baton.sessions.resolve", return_value=task()), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["handoff", "--from", "codex", "--to", "dsh", "--root", directory, "--state", str(state_path), "--json"]), 0)
            identifier = json.loads(output.getvalue())["id"]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cli.main(["inbox", "--host", "dsh", "--root", directory, "--json"])
            self.assertEqual(json.loads(output.getvalue())[0]["id"], identifier)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cli.main(["receive", identifier, "--host", "dsh", "--root", directory, "--json"])
            self.assertEqual(json.loads(output.getvalue())["state"]["task"], "双向接手")


class SessionSelectionTest(unittest.TestCase):
    def test_latest_uses_actual_activity_not_stale_mtime_and_skips_children(self):
        with WorkspaceTemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            entries = []
            for identifier, timestamp, mtime, origin in (("active", "2026-11-02T05:00:00Z", 1, "user"),
                    ("closed", "2026-11-01T05:00:00Z", 9999999999, "user"), ("child", "2026-11-03T05:00:00Z", 99999999999, "subagent")):
                path = root / f"{identifier}.jsonl"
                records = [{"type": "session_meta", "payload": {"id": identifier, "thread_source": origin}},
                           {"type": "response_item", "timestamp": timestamp, "payload": {"type": "message", "role": "user", "content": [{"text": "task"}]}}]
                path.write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")
                entries.append({"path": path, "mtime": mtime})
            with patch.object(codex, "list_sessions", return_value=entries):
                self.assertEqual(sessions.resolve("codex").thread_id, "active")
                self.assertEqual([item["id"] for item in sessions.describe("codex")], ["active", "closed"])

    def test_dsh_accepts_exact_id_and_ambiguous_prefix_is_rejected(self):
        threads = [task("dsh", "session-one"), task("dsh", "session-other")]
        entries = [{"path": Path("one"), "thread": threads[0], "activity": 1}, {"path": Path("two"), "thread": threads[1], "activity": 2}]
        with patch("baton.sessions.user_sessions", return_value=entries):
            self.assertEqual(sessions.resolve("dsh", "session-one").thread_id, "session-one")
            with self.assertRaises(ValueError): sessions.resolve("dsh", "session-")


if __name__ == "__main__": unittest.main()
