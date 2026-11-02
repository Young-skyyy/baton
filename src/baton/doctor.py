"""Doctor: prove the parsers still understand the formats on this machine."""

from __future__ import annotations

import json

from . import bundle, render
from .sources import codex, dsh

CODEX_FIXTURE = [
    {"type": "session_meta", "payload": {"id": "doctor", "cwd": "C:/doctor"}},
    {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:00Z",
        "payload": {"type": "message", "role": "user", "content": [{"text": "doctor 自检"}]},
    },
    {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:01Z",
        "payload": {"type": "function_call", "name": "exec", "arguments": "echo hi", "call_id": "c1"},
    },
]

DSH_FIXTURE = [
    {"type": "session", "version": 4, "id": "doctor-dsh", "cwd": "C:/doctor"},
    {
        "type": "user/message",
        "time": 1700000000000,
        "data": {"role": "user", "content": [{"type": "text", "text": "doctor 自检"}]},
    },
    {
        "type": "tool/call",
        "time": 1700000001000,
        "data": {"name": "exec", "arguments": "echo hi", "callId": "c1"},
    },
]


def _line(label: str, ok: bool, detail: str) -> bool:
    print(f"[{'ok  ' if ok else 'FAIL'}] {label}: {detail}")
    return ok


def _warn(label: str, detail: str) -> None:
    print(f"[warn] {label}: {detail}")


def run() -> int:
    healthy = True

    thread = codex.parse_lines(CODEX_FIXTURE, "<fixture:codex>")
    counts = thread.counts()
    healthy &= _line(
        "codex 解析器",
        counts.get("user") == 1 and counts.get("tool_call") == 1,
        f"{counts.get('user', 0)} 轮用户 / {counts.get('tool_call', 0)} 次工具",
    )

    dsh_thread = dsh.parse_lines(DSH_FIXTURE, "<fixture:dsh>")
    dsh_counts = dsh_thread.counts()
    healthy &= _line(
        "dsh 解析器",
        dsh_counts.get("user") == 1 and dsh_counts.get("tool_call") == 1,
        f"{dsh_counts.get('user', 0)} 轮用户 / {dsh_counts.get('tool_call', 0)} 次工具",
    )

    for name, source in (("codex", codex), ("dsh", dsh)):
        sessions = source.list_sessions()
        if not sessions:
            _warn(f"{name} 会话", "本机没有找到任何会话文件")
            continue
        newest = source.parse_file(sessions[0]["path"])
        turns = newest.counts().get("user", 0)
        if turns == 0:
            _warn(f"{name} 会话", f"最新文件解析出 0 轮用户消息 —— 格式可能变了（{sessions[0]['path'].name}）")
        else:
            _line(f"{name} 会话", True, f"{len(sessions)} 个，最新解析出 {turns} 轮")

    return 0 if healthy else 1
