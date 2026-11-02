"""DSH sessions: ~/.dsh/sessions/**/session.v4.jsonl.zstd

The DSH session format is newer than this tool and may change. The reader stays
deliberately tolerant: unknown event types are kept, never dropped.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ..model import Event, Thread, is_injected

ROOT = Path(os.path.expanduser("~")) / ".dsh" / "sessions"

NODE_DECODER = """
const fs = require('fs'), z = require('zlib');
const buf = fs.readFileSync(process.argv[1]);
const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);
const starts = [];
let cursor = 0;
while ((cursor = buf.indexOf(MAGIC, cursor)) !== -1) { starts.push(cursor); cursor += 4; }
if (starts.length === 0) {
  process.stdout.write(z.zstdDecompressSync(buf).toString('utf8'));
  process.exit(0);
}
const parts = [];
let start = starts[0];
for (let k = 1; k <= starts.length; k++) {
  const end = k < starts.length ? starts[k] : buf.length;
  try {
    parts.push(z.zstdDecompressSync(buf.subarray(start, end)).toString('utf8'));
    start = end;
  } catch (err) {
    if (k === starts.length) { console.error(err.message); process.exit(1); }
  }
}
process.stdout.write(parts.join('\\n'));
"""


def _node_binary() -> str | None:
    override = os.environ.get("BATON_NODE")
    if override:
        return override
    found = shutil.which("node")
    if found:
        return found
    for candidate in Path(os.path.expanduser("~")).glob(".dsh/dsh-runtimes/*/dependencies/node/bin/node*"):
        if candidate.is_file():
            return str(candidate)
    return None


def _node_decompress(path: Path) -> bytes:
    node = _node_binary()
    if not node:
        raise RuntimeError("找不到 node；安装 zstandard 或设置 BATON_NODE 后重试")
    proc = subprocess.run([node, "-e", NODE_DECODER, str(path)], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"node 解压失败: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    return proc.stdout


def decompress(path: Path) -> bytes:
    raw = path.read_bytes()
    try:
        import zstandard

        with zstandard.ZstdDecompressor().stream_reader(
            io.BytesIO(raw), read_across_frames=True
        ) as reader:
            return reader.read()
    except Exception:
        pass
    node = _node_binary()
    if node:
        return _node_decompress(path)
    try:
        from compression import zstd  # Python 3.14+, single frame only

        return zstd.decompress(raw)
    except Exception:
        raise RuntimeError("解压 DSH 会话失败：请安装 zstandard，或提供可用的 node（BATON_NODE）")


def _clock(epoch_ms) -> str:
    try:
        return datetime.fromtimestamp(float(epoch_ms) / 1000).strftime("%H:%M:%S")
    except Exception:
        return ""


def _flatten(data) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return payload if len(payload) <= 2000 else payload[:2000] + "…"


def list_sessions(root: Path | None = None) -> list:
    base = root or ROOT
    found = []
    for path in base.rglob("session.v4.jsonl.zstd"):
        stat = path.stat()
        found.append({"path": path, "mtime": stat.st_mtime, "size": stat.st_size})
    found.sort(key=lambda item: item["mtime"], reverse=True)
    return found


def _content_text(content) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str) and part["text"]:
            parts.append(part["text"])
        elif part.get("type") == "image":
            attachment = part.get("attachment")
            name = attachment.get("name") if isinstance(attachment, dict) else "image"
            name = name or "image"
            parts.append(f"[图片: {name}]")
    return "\n".join(parts)


def _is_echo(thread: Thread, kind: str, body: str) -> bool:
    if not thread.events:
        return False
    last = thread.events[-1]
    return last.kind == kind and last.text == body


def parse_lines(items, source: str) -> Thread:
    """Keep every nonblank record; normalized conversation is a projection."""
    thread = Thread(host="dsh", thread_id=Path(source).parent.name, source=source)
    rows = []
    for lineno, obj in enumerate(items, 1):
        if isinstance(obj, str):
            obj = obj.strip()
            if not obj:
                continue
            try:
                obj = json.loads(obj)
            except ValueError:
                rows.append((0, lineno, Event(0, "", "meta", text="无法解析的记录", raw=obj, ref=f"{source}#{lineno}")))
                continue
        if not isinstance(obj, dict):
            rows.append((0, lineno, Event(0, "", "meta", text="非对象记录", raw={"unparsed_record": obj}, ref=f"{source}#{lineno}")))
            continue
        kind = obj.get("type", "")
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        to = _clock(obj.get("time"))
        try:
            stamp = float(obj.get("time") or 0)
        except (ValueError, TypeError):
            stamp = 0
        if kind == "session":
            thread.thread_id = obj.get("id") or thread.thread_id
            thread.cwd = obj.get("cwd") or thread.cwd
        event = None
        if kind == "user/message":
            body = _content_text(data.get("content")).strip()
            if body and not is_injected(body):
                event = Event(0, to, "user", text=body, ref=f"{source}#{lineno}")
        elif kind == "assistant/message":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            body = _content_text(message.get("content")).strip()
            if body:
                event = Event(0, to, "assistant", text=body, ref=f"{source}#{lineno}")
        elif kind == "tool/call":
            event = Event(
                0,
                to,
                "tool_call",
                name=str(data.get("name", "")),
                args=str(data.get("arguments", "")),
                call_id=str(data.get("callId", "")),
                ref=f"{source}#{lineno}",
            )
        elif kind == "tool/result":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            event = Event(
                0,
                to,
                "tool_result",
                result=_content_text(message.get("content")),
                call_id=str(data.get("toolCallId", "")),
                ref=f"{source}#{lineno}",
            )
        if event is None:
            event = Event(0, to, "meta", text=f"原始记录：{kind or 'unknown'}", ref=f"{source}#{lineno}")
        event.raw = obj
        rows.append((stamp, lineno, event))

    rows.sort(key=lambda row: (row[0], row[1]))
    for index, (_, _, event) in enumerate(rows, 1):
        event.seq = index
        if event.kind == "user" and _is_echo(thread, "user", event.text):
            event.kind, event.text = "meta", "重复用户记录（原文保留）"
        thread.events.append(event)
    thread.meta["preservation"] = "每条非空源记录保存在 raw；导出时脱敏；附件只保存引用"
    return thread


def parse_file(path: Path) -> Thread:
    text = decompress(path).decode("utf-8", "replace")
    return parse_lines(text.splitlines(), str(path))


def find_session(token: str, root: Path | None = None) -> Path | None:
    candidate = Path(token)
    if candidate.is_file():
        return candidate
    for entry in list_sessions(root):
        path = entry["path"]
        if token in str(path) or token in path.parent.name:
            return path
    return None
