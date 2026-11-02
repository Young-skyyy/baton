"""Measure the judgement layer on whatever sessions this machine has.

One sample proves nothing. This prints the distribution so a silent rule shows
up as a number instead of looking like a confident answer.
"""

from __future__ import annotations

from collections import Counter

from . import render
from .sources import codex, dsh

SOURCES = {"codex": codex, "dsh": dsh}


def run(host: str = "codex") -> int:
    source = SOURCES[host]
    stats = Counter()
    statuses = Counter()
    unknown = []
    for entry in source.list_sessions():
        thread = source.parse_file(entry["path"])
        if thread.meta.get("thread_source") in ("guardian_review", "subagent"):
            stats["subthread"] += 1
            continue
        turns = thread.counts().get("user", 0)
        if turns == 0:
            stats["empty"] += 1
            continue
        stats["sessions"] += 1
        state = render.assess(thread)
        statuses[state["status"]] += 1
        substantive = bool(state["next_steps"]) and not state["next_steps"][0].startswith("没有明确下一步")
        stats["no_steps"] += 0 if substantive else 1
        stats["no_claims"] += 0 if state["claims"] else 1
        stats["no_constraints"] += 0 if state["constraints"] else 1
        if state["status"] == "unknown":
            unknown.append((turns, thread.thread_id[:8]))

    total = max(stats["sessions"], 1)
    print(f"host: {host}")
    print(f"  会话 {stats['sessions']} 条（跳过子线程 {stats['subthread']} 条、空会话 {stats['empty']} 条）")
    print(f"  状态：✅ {statuses['finished']}  ⏳ {statuses['in_progress']}  ❔ {statuses['unknown']}")
    for key, label in (
        ("no_steps", "没有实质下一步（含兜底）"),
        ("no_claims", "没有完成声明"),
        ("no_constraints", "没有硬约束"),
    ):
        print(f"  {label}: {stats[key]}/{total} = {stats[key] * 100 // total}%")
    if unknown:
        print(f"  判不出的最近 3 条：{', '.join(f'{tid}({turns} 轮)' for turns, tid in unknown[-3:])}")
    return 0
