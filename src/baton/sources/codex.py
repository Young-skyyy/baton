"""Codex sessions: ~/.codex/sessions/**/rollout-*.jsonl"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from ..model import Event, Thread, is_injected

ROOT = Path(os.path.expanduser("~")) / ".codex" / "sessions"

NOISE_MARKERS = ("<environment_context>", "<user_instructions>", "<external_codex_apps_open_page>")
REPLY_MARKER = "<send_user_message_question_reply>"


def _local_clock(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return stamp[11:16]


def _message_text(payload: dict) -> str:
    content = payload.get("content")
    if isinstance(content, list):
        return "".join(part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
    return content if isinstance(content, str) else ""


def _is_noise(text: str) -> bool:
    head = text[:200]
    return is_injected(text) or any(marker in head for marker in NOISE_MARKERS)


def _arg_text(raw) -> str:
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, ensure_ascii=False)


def _normalize_user_text(text: str) -> str:
    """Codex records answers to its own questions as a JSON blob; keep only the answers."""
    if REPLY_MARKER not in text:
        return text
    payload = text.split(REPLY_MARKER, 1)[1].replace("</send_user_message_question_reply>", "").strip()
    try:
        items = json.loads(payload)
    except ValueError:
        return text
    answers = [str(item.get("answer", "")).strip() for item in items if isinstance(item, dict)]
    answers = [answer for answer in answers if answer]
    return "；".join(answers) if answers else text


def list_sessions(root: Path | None = None) -> list:
    base = root or ROOT
    found = []
    for path in base.rglob("rollout-*.jsonl"):
        stat = path.stat()
        found.append({"path": path, "mtime": stat.st_mtime, "size": stat.st_size})
    found.sort(key=lambda item: item["mtime"], reverse=True)
    return found


def parse_lines(items, source: str) -> Thread:
    """Turn parsed rollout records into events. Shared by the file reader and doctor."""
    thread = Thread(host="codex", thread_id="", source=source)
    ordered = []
    for lineno, obj in enumerate(items, 1):
        if isinstance(obj, str):
            if not obj.strip():
                continue
            try:
                obj = json.loads(obj)
            except ValueError:
                ordered.append(("", lineno, Event(0, "", "meta", text="无法解析的记录", raw=obj, ref=f"{source}#{lineno}")))
                continue
        if not isinstance(obj, dict):
            ordered.append(("", lineno, Event(0, "", "meta", text="非对象记录", raw={"unparsed_record": obj}, ref=f"{source}#{lineno}")))
            continue
        kind = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        stamp = str(obj.get("timestamp") or "")
        ts = _local_clock(stamp)
        raw_event = Event(0, ts, "meta", text=f"原始记录：{kind or 'unknown'}", raw=obj, ref=f"{source}#{lineno}")
        if kind == "session_meta":
            thread.thread_id = payload.get("id") or payload.get("session_id") or thread.thread_id
            thread.cwd = payload.get("cwd") or thread.cwd
            thread.meta["thread_source"] = payload.get("thread_source") or ""
            thread.meta["originator"] = payload.get("originator") or ""
            ordered.append((stamp, lineno, raw_event))
            continue
        if kind == "compacted":
            raw_event.text = "上下文被压缩（原始记录保留）"
            ordered.append((stamp, lineno, raw_event))
            continue
        if kind != "response_item":
            ordered.append((stamp, lineno, raw_event))
            continue
        item = payload.get("type")
        event = None
        if item == "reasoning":
            summary = payload.get("summary") or []
            note = " ".join(part.get("text", "") for part in summary if isinstance(part, dict)).strip()
            if note:
                event = Event(0, ts, "note", text=note, ref=f"{source}#{lineno}")
        elif item == "compaction":
            event = Event(0, ts, "meta", text="上下文被压缩（前面可能已丢失）", ref=f"{source}#{lineno}")
        elif item == "message":
            role = payload.get("role")
            text = _message_text(payload).strip()
            if role not in ("user", "assistant") or not text or _is_noise(text):
                ordered.append((stamp, lineno, raw_event))
                continue
            if role == "user":
                text = _normalize_user_text(text)
            event = Event(0, ts, role, text=text, ref=f"{source}#{lineno}")
        elif item in ("function_call", "custom_tool_call", "local_shell_call"):
            event = Event(
                0,
                ts,
                "tool_call",
                name=payload.get("name") or item,
                args=_arg_text(payload.get("arguments") or payload.get("input") or ""),
                call_id=payload.get("call_id") or "",
                ref=f"{source}#{lineno}",
            )
        elif item in ("function_call_output", "custom_tool_call_output"):
            event = Event(
                0,
                ts,
                "tool_result",
                result=_arg_text(payload.get("output")),
                call_id=payload.get("call_id") or "",
                ref=f"{source}#{lineno}",
            )
        if event is None:
            event = raw_event
        event.raw = obj
        ordered.append((stamp, lineno, event))

    ordered.sort(key=lambda row: (row[0], row[1]))
    for index, (_, _, event) in enumerate(ordered, 1):
        event.seq = index
        thread.events.append(event)
    if not thread.thread_id:
        thread.thread_id = Path(source).stem
    thread.meta["preservation"] = "每条非空源记录保存在 raw；导出时脱敏；附件只保存引用"
    return thread


def parse_file(path: Path) -> Thread:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return parse_lines(handle, str(path))


def find_session(token: str, root: Path | None = None) -> Path | None:
    candidate = Path(token)
    if candidate.is_file():
        return candidate
    for entry in list_sessions(root):
        path = entry["path"]
        if token in str(path) or token in path.stem:
            return path
    return None
