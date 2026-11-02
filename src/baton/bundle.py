"""Bundle: the on-disk handoff package. Plain files, no server, no database."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .model import Event, Thread

FORMAT = 2
MANIFEST = "MANIFEST.json"
OUTLINE = "THREAD.md"
BRIEF = "BRIEF.md"
RESUME = "RESUME.md"
ENV = "ENV.md"
STATE = "STATE.json"
EVENTS = "events.jsonl"


def write(thread: Thread, outdir: Path, outline: str, brief_text: str = "", resume_text: str = "", env_text: str = "") -> Path:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(event.to_dict(), ensure_ascii=False) for event in thread.events]
    body = ("\n".join(lines) + "\n").encode("utf-8")
    (outdir / EVENTS).write_bytes(body)
    (outdir / OUTLINE).write_text(outline, encoding="utf-8")
    for name, text in ((BRIEF, brief_text), (RESUME, resume_text), (ENV, env_text)):
        path = outdir / name
        if text:
            path.write_text(text, encoding="utf-8")
        elif path.exists():
            path.unlink()
    manifest = {
        "baton": FORMAT,
        "host": thread.host,
        "thread": thread.thread_id,
        "cwd": thread.cwd,
        "source": thread.source,
        "metadata": thread.meta,
        "events": len(thread.events),
        "sha256": hashlib.sha256(body).hexdigest(),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (outdir / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return outdir


def read(path: Path) -> dict:
    path = Path(path)
    manifest = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    raw = (path / EVENTS).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != manifest.get("sha256"):
        raise ValueError("events.jsonl 与 MANIFEST 的 sha256 不一致，包可能被改动过")
    events = [Event.from_dict(json.loads(line)) for line in raw.decode("utf-8").splitlines() if line.strip()]
    manifest["_events"] = events
    manifest["_outline"] = (path / OUTLINE).read_text(encoding="utf-8")
    brief_path = path / BRIEF
    manifest["_brief"] = brief_path.read_text(encoding="utf-8") if brief_path.exists() else ""
    resume_path = path / RESUME
    manifest["_resume"] = resume_path.read_text(encoding="utf-8") if resume_path.exists() else ""
    env_path = path / ENV
    manifest["_env"] = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    state_path = path / STATE
    manifest["_state"] = state_path.read_text(encoding="utf-8-sig") if state_path.exists() else ""
    return manifest
