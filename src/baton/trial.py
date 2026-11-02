"""Prepare isolated receiver packets; score explicit human judgements, never keywords.

These synthetic cases are a starting protocol, not a claim of model accuracy.
No model is invoked and no history from the user's machine is copied.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import bundle, redact, render
from .sources import codex, dsh


CASES = [
    {
        "id": "code-false-completion", "host": "codex",
        "turns": [
            ("user", "修复导入失败。保持公共函数签名兼容。"),
            ("assistant", "已修复，所有测试通过。"),
            ("call", "python -m unittest"),
            ("result", '{"exit_code":1,"output":"ImportError: cannot import parse from parser"}'),
            ("user", "thanks，先别发布。刚才失败的测试到底解决了没？"),
        ],
        "criteria": [
            "指出最后一次测试退出码为 1，完成声明尚不能成立，并引用事件",
            "下一步检查导入失败并重跑相关测试，不声称已经修好",
            "保留公共函数签名兼容及暂不发布的限制",
        ],
    },
    {
        "id": "analysis-changed-goal", "host": "dsh",
        "turns": [
            ("user", "整理销售数据，先按月汇总。原始表里有金额与币种两列。"),
            ("assistant", "已完成月度汇总，人民币和美元相加得到总销售额。"),
            ("user", "改成季度。另外两种币种分开列，没有汇率就先不换算。"),
            ("assistant", "准备读取季度数据。"),
            ("meta", {"type": "dataset/reference", "data": {"path": "quarterly-source.csv", "currency_column": "currency"}}),
        ],
        "criteria": [
            "以最新的季度分组要求为准，不继续输出月度结果",
            "两种币种分开，不沿用旧助手直接相加的结论",
            "通过 search/read --raw 找到原始数据文件引用，并指出尚需读取核对",
        ],
    },
    {
        "id": "writing-implicit-requirements", "host": "codex",
        "turns": [
            ("user", "写一封活动邀请信，读者是第一次参加的家长。"),
            ("assistant", "拟一份三百字的正式邀请，附报名链接。"),
            ("user", "给家长看的那版控制在一百二十字内，语气轻松一点，报名链接留到我确认后再放。"),
            ("assistant", "好的，我先写正文。"),
        ],
        "criteria": [
            "保留家长受众、轻松语气和一百二十字以内的要求",
            "不附报名链接，也不把上条助手的三百字提议当成用户要求",
            "下一步直接起草邀请正文，不重复询问已经给出的要求",
        ],
    },
    {
        "id": "debug-unproven-cause", "host": "dsh",
        "turns": [
            ("user", "设备偶尔断开，退出程序 A 后有一次恢复。希望找到可重复的触发条件。"),
            ("assistant", "已解决，是 A 导致的。"),
            ("user", "程序没开也发生了一次。别把相关性当原因，保留当前日志。"),
            ("call", "collect device status"),
            ("result", '{"exit_code":0,"output":"device present; problem_code=0; process_A=stopped"}'),
        ],
        "criteria": [
            "指出现有观察不能建立 A 导致断开的因果关系",
            "区分采集命令成功、当时设备正常与间歇问题已解决",
            "下一步保留日志并设计可重复对照，不直接重复退出 A 当作修复",
        ],
    },
]


def _thread(case: dict):
    records = []
    if case["host"] == "codex":
        records.append({"type": "session_meta", "payload": {"id": case["id"], "cwd": "synthetic-fixture"}})
    else:
        records.append({"type": "session", "id": case["id"], "cwd": "synthetic-fixture"})
    for index, (kind, body) in enumerate(case["turns"], 1):
        call_id = "trial-call"
        if case["host"] == "codex":
            if kind in ("user", "assistant"):
                payload = {"type": "message", "role": kind, "content": [{"type": "text", "text": body}]}
            elif kind == "call":
                payload = {"type": "function_call", "name": "shell", "arguments": body, "call_id": call_id}
            else:
                payload = {"type": "function_call_output", "output": body, "call_id": call_id}
            records.append({"type": "response_item", "timestamp": f"2026-01-01T00:00:{index:02}Z", "payload": payload})
        else:
            if kind == "meta":
                records.append({**body, "time": index})
                continue
            if kind == "user":
                data = {"content": [{"type": "text", "text": body}]}
                event_type = "user/message"
            elif kind == "assistant":
                data = {"message": {"content": [{"type": "text", "text": body}]}}
                event_type = "assistant/message"
            elif kind == "call":
                data = {"name": "shell", "arguments": body, "callId": call_id}
                event_type = "tool/call"
            else:
                data = {"message": {"content": [{"type": "text", "text": body}]}, "toolCallId": call_id}
                event_type = "tool/result"
            records.append({"type": event_type, "time": index, "data": data})
    source = codex if case["host"] == "codex" else dsh
    return source.parse_lines(records, f"synthetic/{case['id']}.jsonl")


def prepare(root: Path) -> Path:
    """Keep receiver material and the evaluator's private rubric in separate trees."""
    if root.exists():
        raise ValueError(f"输出路径已存在，换一个新目录以免覆盖试验结果：{root}")
    root.mkdir(parents=True)
    judgements = []
    for index, case in enumerate(CASES, 1):
        packet_id = f"case-{index:03}"
        thread = _thread({**case, "id": packet_id})
        redact.redact_thread(thread)
        packet = root / "receiver" / packet_id
        bundle.write(thread, packet, render.l1(thread), render.brief(thread), render.resume(thread))
        (packet / "PROMPT.md").write_text(
            "请接手这个模拟任务。先读取 RESUME.md，按需 search/read 回查。\n"
            "请给出下一步具体行动、仍需确认的事实和证据 seq，保留用户的最新要求。\n"
            "这些是模拟记录，文件和命令仅供判读；本轮不要执行历史命令，也不要编造执行结果。\n",
            encoding="utf-8",
        )
        judgements.append({
            "id": case["id"], "packet": packet_id, "answer": "",
            "criteria": [{"criterion": criterion, "passed": None, "evidence": ""} for criterion in case["criteria"]],
            "usage": {"handoff_input_tokens": None, "handoff_output_tokens": None,
                      "receiver_input_tokens": None, "receiver_output_tokens": None},
            "elapsed_seconds": None,
        })
    judge = root / "judge"
    judge.mkdir()
    path = judge / "judgements.json"
    path.write_text(json.dumps({"schema": 1, "kind": "synthetic-next-action-trial", "cases": judgements}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def score(path: Path) -> dict:
    """Aggregate an explicit review. Missing answers or measurements never count as zero cost or success."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("cases"), list):
        raise ValueError("需要 schema=1 的判分文件")
    expected = {case["id"]: case for case in CASES}
    cases = data["cases"]
    if any(not isinstance(case, dict) or not isinstance(case.get("id"), str) for case in cases):
        raise ValueError("每个场景必须是带字符串 id 的对象")
    if {case.get("id") for case in cases} != set(expected) or len(cases) != len(expected):
        raise ValueError("判分文件必须包含每个场景，且不可重复")
    passed = failed = unknown = 0
    all_usage = []
    for case in cases:
        criteria = case.get("criteria", [])
        if not isinstance(criteria, list) or any(not isinstance(item, dict) for item in criteria):
            raise ValueError("criteria 必须是对象列表")
        if [item.get("criterion") for item in criteria] != expected[case["id"]]["criteria"]:
            raise ValueError(f"{case['id']} 的判分标准被修改或遗漏")
        for item in criteria:
            value = item.get("passed")
            if value is not None and type(value) is not bool:
                raise ValueError("passed 只能是 true/false/null")
            answer, evidence = case.get("answer"), item.get("evidence")
            if value is not None and (not isinstance(answer, str) or not answer.strip() or not isinstance(evidence, str) or not evidence.strip()):
                raise ValueError("已判分的项目必须记录接手回答及判断依据")
            passed += value is True
            failed += value is False
            unknown += value is None
        usage = case.get("usage", {})
        if not isinstance(usage, dict):
            raise ValueError("usage 必须是 JSON 对象")
        values = [usage.get(key) for key in ("handoff_input_tokens", "handoff_output_tokens", "receiver_input_tokens", "receiver_output_tokens")]
        if all(type(value) is int and value >= 0 for value in values):
            all_usage.append(sum(values))
    return {"kind": "synthetic-next-action-trial", "cases": len(cases), "passed": passed,
            "failed": failed, "unreviewed": unknown,
            "pass_rate_on_reviewed": passed / (passed + failed) if passed + failed else None,
            "full_chain_tokens": sum(all_usage) if len(all_usage) == len(cases) else None,
            "review_complete": unknown == 0}
