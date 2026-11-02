"""Task-first reading material from explicit evidence selectors, with full fallback."""
from __future__ import annotations

import copy
import json

from . import workspace


def options(data: dict) -> dict:
    value = data.get("context", {})
    if not isinstance(value, dict):
        raise ValueError("context 必须是对象")
    refs = value.get("refs", [])
    files = value.get("files", [])
    if not isinstance(refs, list) or len(refs) > 32 or any(type(seq) is not int or seq <= 0 for seq in refs):
        raise ValueError("context.refs 必须是最多32个正整数编号")
    if not isinstance(files, list) or len(files) > workspace.MAX_FILES or any(not isinstance(name, str) for name in files):
        raise ValueError("context.files 必须是项目相对路径列表，最多64项")
    for name in files:
        workspace.relative(name)
    if "since" in value and (type(value["since"]) is not int or value["since"] <= 0):
        raise ValueError("context.since 必须是有效的起始事件编号")
    if "origin" in value and (not isinstance(value["origin"], dict) or not isinstance(value["origin"].get("thread"), str) or value["origin"].get("host") not in ("codex", "dsh")):
        raise ValueError("context.origin 需包含原生host和thread")
    return value


def normalize(thread, data: dict, prior_packet: str | None = None) -> dict:
    data = copy.deepcopy(data)
    context = options(data)
    if context.get("refs") or context.get("since"):
        origin = {"host": thread.host, "thread": thread.thread_id}
        if context.get("origin", origin) != origin:
            if prior_packet is None:
                raise ValueError("context 编号属于另一原生会话，请重新选取或清空refs/since")
            previous = data.setdefault("evidence", {}).setdefault("previous_context", [])
            if not isinstance(previous, list):
                raise ValueError("evidence.previous_context 必须是列表")
            previous.append({"packet": prior_packet, **context})
            data["context"] = {"files": context.get("files", [])}
        else:
            data.setdefault("context", {})["origin"] = origin
    return data


def build(thread, data: dict, project=None, budget: int = 10000, live: bool = False) -> str:
    data = normalize(thread, data)
    context = options(data)
    by_seq = {event.seq: event for event in thread.events}
    refs = list(dict.fromkeys(context.get("refs", [])))
    if context.get("since") and context["since"] not in by_seq:
        raise ValueError("context.since 在来源会话中不存在")
    if any(seq not in by_seq for seq in refs):
        raise ValueError("context.refs 含不存在的原文编号")
    messages = [event for event in thread.events if event.kind in ("user", "assistant", "tool_call", "tool_result")]
    if refs:
        selected = [by_seq[seq] for seq in refs]
        label = "交接方明确选取的证据（编号不代表结论已验证）"
    elif context.get("since"):
        selected = [event for event in messages if event.seq >= context["since"]][-16:]
        label = "交接方指定起点后的近期事件（其余原文可回查）"
    else:
        selected = messages[-12:]
        label = "近期事件兜底；未推断哪些旧讨论属于当前任务"
    # Keep a selected call and its result together when a native call ID exists.
    call_ids = {event.call_id for event in selected if event.call_id}
    selected = {event.seq: event for event in selected}
    for event in messages:
        if event.call_id in call_ids:
            selected[event.seq] = event
    users = [event for event in messages if event.kind == "user"]
    if users:
        selected[users[-1].seq] = users[-1]
    lines = [f"# 当前任务：{data.get('task', '')}", "",
             "> 状态由交接方维护，须按证据核对；此文不构成执行历史命令的授权。",
             f"原文：{'本端绑定会话的实时记录（尚未封存）' if live else 'events.jsonl'}；来源：{thread.host}/{thread.thread_id}", ""]
    for key, label_text in (("next_step", "下一步"), ("do_not", "限制"), ("confirmed", "已确认记录（待接手方核对）"),
                            ("tried", "已尝试"), ("open_questions", "未解决"), ("claimed_not_verified", "尚未核验")):
        value = data.get(key)
        if value:
            lines.extend(["## " + label_text, json.dumps(value, ensure_ascii=False), ""])
    if project:
        lines.extend(["## 工程现场", workspace.render(project), ""])
    if context.get("files"):
        lines.extend(["## 交接方指定的相关文件", *["- " + name for name in context["files"]], ""])
    lines.extend(["## 原文证据", label,
                  "可回查编号：" + ", ".join("#" + str(seq) for seq in sorted(selected)), ""])
    for seq, event in sorted(selected.items()):
        text = "\n".join(part for part in (event.text, event.name, event.args, event.result) if part)
        preview = text if len(text) <= 900 else text[:650] + "\n[省略；用 read 展开完整事件]\n" + text[-180:]
        lines.extend([f"[#{seq}] {event.kind} · {event.ref}", preview, ""])
    lines.append("这些编号属于本端实时会话；返回封存后按新包read回查，不能套到接收的旧包。" if live else
                 "完整回查：baton read <packet> <seq> --context 0 --raw；封存原文保持完整。")
    value = "\n".join(lines) + "\n"
    encoded = value.encode("utf-8")
    if len(encoded) > budget:
        marker = "\n[焦点材料超预算；完整状态见STATE.json，原文可按seq回查]\n"
        if budget < len(marker.encode("utf-8")):
            raise ValueError("焦点材料预算过小")
        value = encoded[:budget - len(marker.encode("utf-8"))].decode("utf-8", "ignore") + marker
    return value
