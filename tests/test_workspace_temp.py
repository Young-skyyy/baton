import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from support import WorkspaceTemporaryDirectory

TMP = Path(__file__).resolve().parents[1] / ".tmp"
TMP.mkdir(exist_ok=True)


class WorkspaceTempTest(unittest.TestCase):
    def test_unique_writable_fixtures_cleanup_only_their_own_tree(self):
        first = WorkspaceTemporaryDirectory(dir=TMP)
        second = WorkspaceTemporaryDirectory(dir=TMP)
        self.addCleanup(first.cleanup); self.addCleanup(second.cleanup)
        path = Path(first.name); other = Path(second.name)
        self.assertNotEqual(path, other)
        (path / "nested").mkdir()
        (path / "nested/value.txt").write_text("fixture", encoding="utf-8")
        (other / "keep.txt").write_text("sibling", encoding="utf-8")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        first.cleanup(); first.cleanup()
        self.assertFalse(path.exists())
        self.assertEqual((other / "keep.txt").read_text(encoding="utf-8"), "sibling")
        self.assertTrue(TMP.is_dir())

    def test_exception_still_cleans_up_fixture(self):
        with self.assertRaisesRegex(RuntimeError, "fixture failed"):
            with WorkspaceTemporaryDirectory(dir=TMP) as directory:
                created = Path(directory)
                (created / "output.txt").write_text("partial", encoding="utf-8")
                raise RuntimeError("fixture failed")
        self.assertFalse(created.exists())

    def test_cleanup_refuses_a_changed_resolved_target(self):
        fixture = WorkspaceTemporaryDirectory(dir=TMP)
        self.addCleanup(fixture.cleanup)
        with patch.object(Path, "resolve", return_value=TMP.resolve().parent), \
                patch("support.shutil.rmtree") as remove:
            with self.assertRaisesRegex(ValueError, "refusing"):
                fixture.cleanup()
            remove.assert_not_called()
        self.assertTrue(Path(fixture.name).is_dir())


if __name__ == "__main__":
    unittest.main()
