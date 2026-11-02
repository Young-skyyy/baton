"""State file: the durable, executable part of a handoff.

The tool scaffolds it from evidence and validates it; the agent that hands off
fills in the judgement. Rules can find the shape, only a model knows the meaning.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import Thread, one_line

REQUIRED = ("task", "status", "next_step")
STATUSES = ("paused", "done", "blocked")


def skeleton(thread: Thread, state: dict) -> dict:
    claimed = [f"{claim['text']} [#{claim['seq']}]" for claim in state.get("claims", [])]
    forbid = [f"{item['text']} [#{item['seq']}]" for item in state.get("constraints", []) if item["kind"] == "forbid"]
    next_step = state.get("next_steps", [""])[0] if state.get("next_steps") else ""
    return {
        "task": one_line(thread.first_user(), 120),
        "status": "paused",
        "confirmed": [],
        "claimed_not_verified": claimed,
        "tried": [],
        "open_questions": [],
        "next_step": next_step,
        "do_not": forbid,
        "evidence": {},
    }


def write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def check(path: Path) -> list:
    if not path.exists():
        return ["STATE.json 不存在"]
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as error:
        return [f"不是合法 JSON：{error}"]
    return problems(data)


def problems(data) -> list:
    if not isinstance(data, dict):
        return ["STATE.json 必须是 JSON 对象"]
    problems = []
    for key in REQUIRED:
        if not isinstance(data.get(key), str) or not data[key].strip():
            problems.append(f"`{key}` 必须是非空字符串")
    if data.get("status") not in STATUSES:
        problems.append(f"status 必须是 {'/'.join(STATUSES)}，当前 {data.get('status')!r}")
    if data.get("next_step") and len(str(data["next_step"])) > 400:
        problems.append("next_step 超过 400 字，接手方读不完")
    for key in ("confirmed", "claimed_not_verified", "tried", "open_questions", "do_not"):
        value = data.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            problems.append(f"`{key}` 必须是字符串列表")
    if not isinstance(data.get("evidence", {}), dict):
        problems.append("`evidence` 必须是 JSON 对象")
    from . import focus
    try:
        focus.options(data)
    except ValueError as error:
        problems.append(str(error))
    if "project" in data and (not isinstance(data["project"], str) or not data["project"].strip()):
        problems.append("project 必须是非空工程路径")
    checks = data.get("checks", [])
    if not isinstance(checks, list) or len(checks) > 32 or any(
            not isinstance(item, dict) or not isinstance(item.get("command"), str) or not item["command"].strip()
            or (item.get("exit_code") is not None and type(item["exit_code"]) is not int)
            or (item.get("log") is not None and not isinstance(item["log"], str)) for item in checks):
        problems.append("checks 必须是最多32条 command/exit_code/log 测试记录；仅作交接方填报")
    return problems
