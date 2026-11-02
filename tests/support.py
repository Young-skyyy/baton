"""Temporary fixtures that stay inside the chosen writable workspace.

Windows Python 3.12.10+ creates owner-only ACLs for mode 0700, which can
exclude a host's restricted token. Inherit the existing parent ACL instead.
Unix fixtures retain private mode 0700. No existing permissions are changed.
"""
import os
import shutil
import uuid
from pathlib import Path


class WorkspaceTemporaryDirectory:
    def __init__(self, *, dir):
        self._parent = Path(dir).resolve(strict=True)
        if not self._parent.is_dir():
            raise ValueError("Fixture parent must be an existing directory")
        self._path = self._parent / ("baton-test-" + uuid.uuid4().hex)
        self._path.mkdir(mode=0o777 if os.name == "nt" else 0o700)
        self.name = str(self._path)
        self._closed = False

    def cleanup(self):
        if self._closed:
            return
        # Never follow a replaced fixture root or clean outside its chosen parent.
        if self._path.resolve() != self._path or self._path.parent != self._parent:
            raise ValueError("Fixture path changed; refusing recursive cleanup")
        if self._path.exists():
            shutil.rmtree(self._path)
        self._closed = True

    def __enter__(self):
        return self.name

    def __exit__(self, *_):
        self.cleanup()
