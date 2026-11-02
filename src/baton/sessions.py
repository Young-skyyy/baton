"""Find an actual user session, including an active file whose mtime is stale."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import redact
from .sources import codex, dsh

SOURCES = {"codex": codex, "dsh": dsh}


def _last_activity(thread, fallback):
    activity = float(fallback)
    timestamps = []
    for event in thread.events:
        raw = event.raw
        if not isinstance(raw, dict):
            continue
        stamp = raw.get("timestamp")
        try:
            if isinstance(stamp, str):
                timestamps.append(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
            elif isinstance(raw.get("time"), (int, float)):
                timestamps.append(float(raw["time"]) / 1000)
        except (ValueError, OverflowError, OSError):
            pass
    return max(timestamps) if timestamps else activity


def user_sessions(host: str) -> list:
    source = SOURCES[host]
    result = []
    for entry in source.list_sessions():
        path = entry["path"]
        if host == "codex":
            # Skip the many child logs before parsing their potentially huge payloads.
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("type") == "session_meta":
                        meta = row.get("payload") or {}
                        origin = meta.get("source")
                        is_subagent_source = isinstance(origin, dict) and "subagent" in origin
                        if meta.get("thread_source") in ("guardian_review", "subagent") or is_subagent_source:
                            path = None
                        break
        if path is None:
            continue
        thread = source.parse_file(path)
        if thread.meta.get("thread_source") in ("guardian_review", "subagent") or not thread.counts().get("user"):
            continue
        result.append({"thread": thread, "activity": _last_activity(thread, entry["mtime"]), "path": path})
    return sorted(result, key=lambda item: (item["activity"], str(item["path"])), reverse=True)


def resolve(host: str, token: str = "latest"):
    if host not in SOURCES:
        raise ValueError("host 必须是 codex 或 dsh")
    source = SOURCES[host]
    if token != "latest" and Path(token).is_file():
        thread = source.parse_file(Path(token))
    else:
        candidates = user_sessions(host)
        if token == "latest":
            matches = candidates[:1]
        else:
            exact = [item for item in candidates if item["thread"].thread_id == token or item["path"].parent.name == token]
            matches = exact or [item for item in candidates if token in item["thread"].thread_id or token in str(item["path"])]
        if not matches:
            raise ValueError(f"找不到 {host} 用户会话：{token}")
        if len(matches) != 1:
            raise ValueError("会话片段不唯一，请使用完整 ID 或文件路径")
        thread = matches[0]["thread"]
    if thread.meta.get("thread_source") in ("guardian_review", "subagent"):
        raise ValueError("不能把子代理或审查会话当作用户任务")
    return thread


def describe(host: str, limit: int = 10) -> list:
    result = []
    for item in user_sessions(host)[:limit]:
        thread = item["thread"]
        redact.redact_thread(thread)
        result.append({"id": thread.thread_id, "host": host, "cwd": thread.cwd,
                       "first_user": thread.first_user()[:160], "last_user": thread.last_user()[:160],
                       "activity": datetime.fromtimestamp(item["activity"], timezone.utc).isoformat(),
                       "path": str(item["path"]), "user_turns": thread.counts().get("user", 0)})
    return result
