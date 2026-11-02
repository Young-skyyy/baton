"""Exercise all transient writers near Windows' legacy path-length limit."""
import json
import sys
import unittest
from pathlib import Path

from support import WorkspaceTemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from baton import bridge, flow
from baton.model import Event, Thread

TMP = Path(__file__).resolve().parents[1] / ".tmp"
TMP.mkdir(exist_ok=True)


class LongMailboxTest(unittest.TestCase):
    def test_send_receive_draft_and_return_under_a_187_character_root(self):
        with WorkspaceTemporaryDirectory(dir=TMP) as directory:
            base = Path(directory).resolve()
            remaining = 187 - len(str(base)) - 1
            if remaining < 1:
                self.skipTest("Test checkout itself needs a shorter directory")
            root = base / ("x" * remaining)
            root.mkdir()
            self.assertEqual(len(str(root)), 187)
            source = Thread("codex", "fixture-codex", events=[Event(1, "", "user", text="Continue")])
            receiver = Thread("dsh", "fixture-dsh", events=[Event(1, "", "user", text="Receive")])
            state = {"task": "Long mailbox", "status": "paused", "next_step": "Review"}
            sent = bridge.send(root, source, "dsh", state)
            accepted = flow.take(root, "dsh", sent["id"], thread=receiver)
            draft = Path(accepted["state_path"])
            value = json.loads(draft.read_text(encoding="utf-8"))
            value["next_step"] = "Updated draft"
            draft.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(flow.take(root, "dsh")["state"]["next_step"], "Updated draft")
            back = flow.reply(root, "dsh", receiver)
            received = flow.take(root, "codex", back["id"], thread=source)
            self.assertEqual(received["state"]["next_step"], "Updated draft")
            self.assertEqual(received["chain"], sent["chain"])
            self.assertTrue(bridge.inspect(root, sent["id"])["receipt"])
            self.assertTrue(bridge.inspect(root, back["id"])["receipt"])
            self.assertFalse(any(path.name.startswith(".p-") for path in (root / "items").iterdir()))
