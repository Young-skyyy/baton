"""Four densities of the same thread: one line, a brief, an outline, the full stream."""

from __future__ import annotations

import json
import re

from . import conflicts
from .model import Thread, one_line

CMD_PATTERN = re.compile(r'cmd\s*:\s*("(?:[^"\\]|\\.)*")')


def clean_args(raw: str) -> str:
    """Codex wraps shell calls in JS; the command string is the part worth keeping."""
    match = CMD_PATTERN.search(raw or "")
    if match:
        try:
            return json.loads(match.group(1))
        except ValueError:
            return raw or ""
    return raw or ""


def l0(thread: Thread) -> str:
    counts = thread.counts()
    return (
        f"[{thread.host}] {thread.thread_id[:8]} | "
        f"{counts.get('user', 0)} 轮 / {counts.get('tool_call', 0)} 次工具 | "
        f"起：{one_line(thread.first_user(), 50)} | "
        f"止：{one_line(thread.last_assistant() or thread.last_user(), 50)}"
    )


def _fence(text: str, limit: int) -> str:
    clipped = text if len(text) <= limit else text[:limit] + "\n… (截断，见 events.jsonl)"
    return f"```\n{clipped}\n```"


def _fit_bytes(text: str, budget: int) -> str:
    if budget < 0:
        raise ValueError("budget 必须非负")
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    suffix = "\n…（已截断）\n"
    if budget < len(suffix.encode("utf-8")):
        return raw[:budget].decode("utf-8", "ignore")
    cut = raw[:budget - len(suffix.encode("utf-8"))].decode("utf-8", "ignore").rstrip()
    return cut + suffix


def _clip_text(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def l1(thread: Thread) -> str:
    out = [f"# 交接：{one_line(thread.first_user(), 70) or thread.thread_id}", ""]
    out.append(f"- host: `{thread.host}`")
    out.append(f"- thread: `{thread.thread_id}`")
    if thread.cwd:
        out.append(f"- cwd: `{thread.cwd}`")
    counts = thread.counts()
    out.append(
        f"- 规模: {counts.get('user', 0)} 轮用户 / {counts.get('assistant', 0)} 轮助手 / "
        f"{counts.get('tool_call', 0)} 次工具 / {counts.get('tool_result', 0)} 次返回"
    )
    out.append("")
    out.append("## 时间线")
    for event in thread.events:
        if event.kind == "user":
            out.extend(["", f"### [{event.ts}] 用户", "", event.text.strip()])
        elif event.kind == "assistant":
            out.extend(["", f"### [{event.ts}] 助手", "", _clip_text(event.text, 700)])
        elif event.kind == "tool_call":
            out.extend(["", f"### [{event.ts}] 工具 `{event.name}`", "", _fence(clean_args(event.args), 300)])
        elif event.kind == "note":
            out.extend(["", f"### [{event.ts}] 思考 · {event.text}"])
        elif event.kind == "meta":
            if "压缩" in event.text:
                out.extend(["", f"### [{event.ts}] {event.text}"])
    out.append("")
    return "\n".join(out)


def l2(thread: Thread) -> str:
    out = [l1(thread), "", "## 工具返回预览（完整内容使用 read）"]
    for event in thread.events:
        if event.kind == "tool_result":
            out.extend(["", f"### [{event.ts}] 返回 `{event.call_id or ''}`", "", _fence(event.result, 4000)])
    out.append("")
    return "\n".join(out)


def _files_touched(thread: Thread, limit: int = 8) -> list:
    seen = []
    for event in thread.events:
        if event.kind != "tool_call":
            continue
        flat = (event.args or "").replace("\\n", " ⏎ ")
        for marker in ("*** Update File: ", "*** Add File: ", "*** Delete File: "):
            for chunk in flat.split(marker)[1:]:
                name = chunk.split(" ⏎ ")[0].strip()
                if name and name not in seen:
                    seen.append(name)
    return seen[-limit:]


PENDING_MARKERS = (
    "我先",
    "接下来",
    "下一步",
    "准备",
    "会先",
    "稍后",
    "正在",
    "等待",
    "继续",
    "let me",
    "next step",
    "I'll",
    "I will",
    "will now",
    "plan to",
)
FINISHED_MARKERS = (
    "已完成",
    "已经完成",
    "已按",
    "已交付",
    "已生成",
    "都改好了",
    "完成了",
    "已修复",
    "已解决",
    "解决了",
    "修复了",
    "已改好",
    "done",
    "completed",
    "fixed",
    "implemented",
    "shipped",
    "finished",
    "all set",
)
QUESTION_PATTERN = re.compile(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _question_title(args: str) -> str:
    match = QUESTION_PATTERN.search(args or "")
    if not match:
        return ""
    try:
        return json.loads('"' + match.group(1) + '"')
    except ValueError:
        return ""


def _steps_from(text: str, limit: int = 3) -> list:
    steps = []
    for raw in re.split(r"[。！？\n]", text or ""):
        sentence = raw.strip(" ，,;；")
        if sentence and any(marker in sentence for marker in PENDING_MARKERS):
            steps.append(one_line(sentence, 90))
        if len(steps) >= limit:
            break
    return steps


def _reaction(thread: Thread, after: int):
    """Locate the next user turn, without guessing what it confirms or refutes."""
    for event in thread.events:
        if event.seq <= after or event.kind != "user":
            continue
        return {"seq": event.seq, "text": event.text}
    return None


FORBID_MARKERS = (
    "不要",
    "不需要",
    "不用",
    "不能",
    "没必要",
    "不想",
    "别加",
    "别做",
    "别用",
    "别改",
    "别写",
    "别弄",
    "别给",
    "don't",
    "do not",
    "no need",
    "never",
)
REQUIRE_MARKERS = ("必须", "一定要", "务必", "记得", "只需要", "只能", "要求", "must", "make sure", "always")


def _constraints(thread: Thread, limit: int = 6) -> list:
    """What the user explicitly forbade or demanded — the part a fresh agent breaks first."""
    found = []
    for event in thread.events:
        if event.kind != "user":
            continue
        for sentence in re.split(r"[。！？\n]", event.text):
            text = sentence.strip(" ，,;；")
            if len(text) < 4:
                continue
            probe = text
            for filler in ("能不能", "可不可以", "要不要", "是不是", "会不会"):
                probe = probe.replace(filler, "")
            forbids = any(marker in probe for marker in FORBID_MARKERS)
            requires = any(marker in probe for marker in REQUIRE_MARKERS)
            if not (forbids or requires):
                continue
            found.append(
                {
                    "text": one_line(text, 80),
                    "seq": event.seq,
                    "kind": "forbid" if forbids else "require",
                }
            )
    unique = []
    seen = set()
    for item in found:
        key = item["text"][:20]
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[-limit:]


EXIT_CODE = re.compile(r'\\?"exit_code\\?"\s*:\s*(-?\d+)')


def exit_codes(text: str) -> list:
    return [int(match) for match in EXIT_CODE.findall(text or "")]


def failed(event) -> bool:
    """A command that returned a non-zero exit code — structure, not vocabulary."""
    return any(code != 0 for code in exit_codes(event.result))


def _bigrams(text: str) -> set:
    flat = re.sub(r"\s+", "", text or "")
    return {flat[i : i + 2] for i in range(len(flat) - 1)}


def _recurring(thread: Thread, limit: int = 2) -> list:
    """The same complaint raised twice — relative signal, no dictionary needed."""
    users = [event for event in thread.events if event.kind == "user" and len(event.text) >= 8]
    signatures = [_bigrams(event.text) for event in users]
    found = []
    for index, first in enumerate(users):
        for other_index in range(index + 1, len(users)):
            again = users[other_index]
            left, right = signatures[index], signatures[other_index]
            if not left or not right:
                continue
            score = len(left & right) / len(left | right)
            if score >= 0.6:
                found.append(
                    {
                        "a": first.seq,
                        "b": again.seq,
                        "text": one_line(again.text, 60),
                        "score": round(score, 2),
                    }
                )
                break
    return found[-limit:]


def _failed_calls(thread: Thread) -> set:
    return {event.call_id for event in thread.events if event.kind == "tool_result" and failed(event)}


def _claims(thread: Thread, limit: int = 3) -> list:
    """Locate possible completion claims. Vocabulary never establishes truth."""
    found = []
    for event in thread.events:
        if event.kind != "assistant":
            continue
        for sentence in re.split(r"[。！？\n]", event.text):
            text = sentence.strip()
            if not text or not any(marker in text for marker in FINISHED_MARKERS):
                continue
            reaction = _reaction(thread, event.seq)
            why = "助手声称完成，尚未核验"
            if reaction:
                why += f"；后续用户回应见 [#{reaction['seq']}]，需核对是否针对该声明"
            found.append(
                {
                    "text": one_line(text, 90),
                    "seq": event.seq,
                    "state": "unverified",
                    "why": why,
                    "reaction": reaction,
                }
            )
    return found[-limit:]


def _pending_question(thread: Thread) -> str:
    """The last question the agent asked, if the user never answered it."""
    ask = None
    for event in thread.events:
        if event.kind == "tool_call" and event.name == "request_user_input_async":
            ask = event
    if ask is None:
        return ""
    answered = any(event.kind == "user" and event.seq > ask.seq for event in thread.events)
    if answered:
        return ""
    return _question_title(ask.args)


def assess(thread: Thread) -> dict:
    """Decide whether the thread stopped mid-task, and what the next move is."""
    conversation = [event for event in thread.events if event.kind in ("user", "assistant", "tool_call", "tool_result")]
    if not conversation:
        return {"status": "unknown", "reason": "没有可识别的对话事件；请检查原始记录", "next_steps": [], "evidence": [], "claims": [], "constraints": [], "recurring": []}
    last = conversation[-1]
    last_assistant = None
    for event in reversed(thread.events):
        if event.kind == "assistant":
            last_assistant = event
            break
    steps = []
    evidence = []
    if last.kind in ("tool_call", "tool_result"):
        what = "工具调用" if last.kind == "tool_call" else "工具返回"
        status, reason = "in_progress", f"最后一条是{what}，之后没有助手收尾"
        evidence.append(last.seq)
    elif last.kind == "user":
        status, reason = "in_progress", "最后一条用户输入尚未处理"
        evidence.append(last.seq)
        steps.append(f"先处理最新用户输入：{one_line(last.text, 140)}")
    elif last_assistant is not None:
        head = last_assistant.text[:200]
        tail = last_assistant.text[-200:]
        evidence.append(last_assistant.seq)
        if any(marker in tail for marker in PENDING_MARKERS):
            status, reason = "in_progress", "最后一条助手消息是计划/进行时"
        elif any(marker in head or marker in tail for marker in FINISHED_MARKERS):
            status, reason = "unknown", "助手声称完成，尚未核验；不能据此确认任务完成"
        else:
            status, reason = "unknown", "最后一条助手消息看不出是否收尾"
        steps.extend(_steps_from(last_assistant.text))
    else:
        status, reason = "in_progress", "只有用户输入，没有助手输出"
        steps.append("从头处理用户的请求")
        evidence.append(last.seq)
    question = _pending_question(thread)
    if question:
        ask = _last_question_seq(thread)
        steps.insert(0, f"等待用户回答：{one_line(question, 80)} [#{ask}]")
        evidence.append(ask)
    elif steps and evidence:
        steps = [f"{step} [#{evidence[-1]}]" for step in steps]
    if not steps:
        recent = "、".join(f"#{event.seq}" for event in conversation[-3:])
        steps = [f"没有明确下一步：先读最近几条（{recent}）确认它到底收尾没有"]
    return {
        "status": status,
        "reason": reason,
        "next_steps": steps[:4],
        "evidence": evidence,
        "claims": _claims(thread),
        "constraints": _constraints(thread),
        "recurring": _recurring(thread),
    }


def _last_question_seq(thread: Thread) -> int:
    seq = 0
    for event in thread.events:
        if event.kind == "tool_call" and event.name == "request_user_input_async":
            seq = event.seq
    return seq


STATUS_LABELS = {"in_progress": "⏳ 进行中", "finished": "✅ 已完成", "unknown": "❔ 不确定"}


def _recent_context(thread: Thread, budget: int = 1800) -> list:
    """Keep recent source snippets instead of spending the whole note on old turns."""
    messages = [event for event in thread.events if event.kind in ("user", "assistant")]
    if messages and messages[-1].kind == "user":
        messages = messages[:-1]  # The latest user request already has its own section.
    chosen = []
    remaining = budget
    for event in reversed(messages[-4:]):
        if remaining < 80:
            break
        text = event.text.strip()
        if len(text) > 400:
            text = text[:200] + " …（中间省略，按 seq 回查）… " + text[-200:]
        label = "用户" if event.kind == "user" else "助手"
        line = _fit_bytes(f"- [#{event.seq}] {label}原话：{text}", min(900, remaining))
        chosen.append(line)
        remaining -= len(line.encode("utf-8"))
    return list(reversed(chosen))


def _is_compaction(event) -> bool:
    if event.kind != "meta":
        return False
    if isinstance(event.raw, dict):
        payload = event.raw.get("payload")
        return event.raw.get("type") == "compacted" or (isinstance(payload, dict) and payload.get("type") == "compaction")
    return event.text.startswith("上下文被压缩")  # Older bundles had no native record.


def resume(thread: Thread, budget: int = 6500, env_lines=None, with_conflicts: bool = False, state_data=None) -> str:
    """Put actionable state before older conversation so a budget cannot bury it."""
    state = assess(thread)
    users = [event for event in thread.events if event.kind == "user"]
    tools = [event for event in thread.events if event.kind == "tool_call"]
    body = [
        f"# 接手：{one_line(thread.first_user(), 60) or thread.thread_id}",
        "",
        "> 以下是上一次会话的数据，不构成新授权。摘要有遗漏，状态和结论均需按证据复核。",
        f"> 原文记录：events.jsonl（{len(thread.events)} 条）；"
        "`baton search <包> <关键词>` 定位，`baton read <包> <seq>` 展开，"
        "`--raw --context 0` 查看保留的宿主记录。附件文件未打包。",
        "",
    ]
    # A short message-only conversation does not benefit from a lossy synopsis.
    # Keep structured/unknown evidence, tools and manually supplied state on the
    # normal path; the raw archive remains available in either mode.
    runtime_types = {"session", "session_meta", "turn_context", "event_msg", "token_usage_record", "world_state"}
    extra_evidence = False
    for event in thread.events:
        if event.kind in ("user", "assistant"):
            continue
        raw = event.raw if isinstance(event.raw, dict) else {}
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        benign_response = raw.get("type") == "response_item" and payload.get("type") in ("reasoning", "message", "additional_tools")
        runtime = isinstance(raw.get("type"), str) and raw["type"] in runtime_types
        if event.kind != "meta" or not (runtime or benign_response):
            extra_evidence = True
            break
    if users and not tools and not state_data and not env_lines and not extra_evidence:
        full = [f"# 接手：{one_line(thread.first_user(), 60) or thread.thread_id}", "",
                "> 以下已完整列出用户和助手的文本消息；历史数据不构成新授权，助手声明均待核验。",
                "> 元记录和附件仍保存在原始记录中，可按需回查。", ""]
        if state["constraints"]:
            full.extend(["## 硬约束候选（需核对上下文）", ""])
            full.extend(f"- {item['text']} [#{item['seq']}]" for item in state["constraints"])
            full.append("")
        full.extend(["## 消息全文（助手声明均待核验）", ""])
        for event in thread.events:
            if event.kind in ("user", "assistant"):
                label = "用户" if event.kind == "user" else "助手"
                full.extend([f"[#{event.seq}] {label}", event.text, ""])
        compact = "\n".join(full).rstrip() + "\n"
        if len(compact.encode("utf-8")) <= budget:
            return compact
    if state_data:
        body.extend(["## 交接人填写的状态（需按证据复核）", "", "完整状态见 STATE.json；以下为预览。"])
        for key, label in (("task", "任务"), ("status", "状态"), ("next_step", "下一步")):
            body.append(f"- {label}：{_clip_text(str(state_data.get(key, '')), 500)}")
        for key, label in (("do_not", "禁止"), ("confirmed", "交接方记录为已确认"),
                           ("tried", "已尝试"), ("open_questions", "待解决"),
                           ("claimed_not_verified", "未核验声明")):
            values = state_data.get(key, [])
            if values:
                body.append(f"- {label}：{_clip_text('；'.join(str(value) for value in values), 600)}")
        if state_data.get("evidence"):
            body.append("- 证据：" + _clip_text(json.dumps(state_data["evidence"], ensure_ascii=False), 800))
        body.append("")
    body.extend(["## 最新用户输入", ""])
    body.append(f"[#{users[-1].seq}] {_clip_text(users[-1].text, 800)}" if users else "（未记录）")
    body.extend(["", "## 当前状态（自动线索，不作完成判定）", ""])
    refs = "、".join(f"#{seq}" for seq in state["evidence"]) or "无"
    body.append(f"{STATUS_LABELS.get(state['status'], state['status'])} —— {state['reason']}（依据 {refs}）")
    body.extend(["", "## 下一步（自动线索）", ""])
    body.extend(f"- {step}" for step in state["next_steps"])
    if state["constraints"]:
        body.extend(["", "## 硬约束候选（需核对上下文）", ""])
        for item in state["constraints"]:
            label = "禁止" if item["kind"] == "forbid" else "要求"
            body.append(f"- [{label}] {item['text']} [#{item['seq']}]")
    if state["claims"]:
        body.extend(["", "## 助手完成声明（全部待核验）", ""])
        for claim in state["claims"]:
            body.append(f"- [未验证] {claim['text']} [#{claim['seq']}] —— {claim['why']}")
    recent = _recent_context(thread)
    if recent:
        body.extend(["", "## 近期原文片段（预览，事实仍需核对）", ""])
        body.extend(recent)
    if env_lines:
        body.extend(["", "## 导出时的环境（接手时重新核对）", ""])
        body.extend(f"- {line}" for line in env_lines)
    if tools:
        body.extend(["", "## 最近工具调用（非零退出不等于任务失败）", ""])
        broken = _failed_calls(thread)
        body.extend(
            f"- [#{event.seq}] [{event.ts}] {'⚠ ' if event.call_id in broken else ''}`{event.name}`: "
            f"{one_line(clean_args(event.args), 90)}" for event in tools[-8:]
        )
    if with_conflicts:
        clashes = conflicts.detect(thread)
        if clashes:
            body.extend(["", "## 冲突候选（实验性，可能误报）", ""])
            for item in clashes:
                body.extend([f"- [#{item['a']['seq']}] {item['a']['text']}",
                             f"  [#{item['b']['seq']}] {item['b']['text']}"])
    if state["recurring"]:
        body.extend(["", "## 相似用户消息（不代表同一问题）", ""])
        for item in state["recurring"]:
            body.append(f"- [#{item['a']}] → [#{item['b']}] 相似度 {item['score']}：{item['text']}")
    body.extend(["", "## 最初目标", "", _clip_text(users[0].text, 400) if users else "（未记录）"])
    if len(users) > 1:
        body.extend(["", "## 较早用户要求（预览，原文可回查）", ""])
        body.extend(f"- [#{event.seq}] {one_line(event.text, 110)}" for event in users[1:-1])
    files = _files_touched(thread)
    if files:
        body.extend(["", "## 涉及文件", ""])
        body.extend(f"- `{name}`" for name in files)
    return _fit_bytes("\n".join(body).rstrip() + "\n", budget)


def brief(thread: Thread, budget: int = 5200) -> str:
    """A budget-bounded handoff note: what was asked, what happened, where it stopped."""
    counts = thread.counts()
    users = [event for event in thread.events if event.kind == "user"]
    tools = [event for event in thread.events if event.kind == "tool_call"]
    compactions = sum(_is_compaction(event) for event in thread.events)
    body = [
        "# 交接简报",
        "",
        f"- host: `{thread.host}` · thread: `{thread.thread_id}`",
        f"- cwd: `{thread.cwd}`",
        f"- 规模: {counts.get('user', 0)} 轮 / {counts.get('tool_call', 0)} 次工具 "
        f"· 最后动作 {tools[-1].ts if tools else '—'}"
        + (f" · 压缩 {compactions} 次" if compactions else ""),
        "",
    ]

    sections = []
    if users:
        sections.append(("## 目标", _clip_text(users[0].text, 400)))
    if len(users) > 1:
        lines = [f"- [{event.ts}] {one_line(event.text, 110)}" for event in users[1:]]
        sections.append(("## 用户后续要求", "\n".join(lines)))
    constraints = _constraints(thread, limit=4)
    if constraints:
        lines = [f"- {item['text']} [#{item['seq']}]" for item in constraints]
        sections.append(("## 硬约束", "\n".join(lines)))
    if tools:
        recent = [f"- [{event.ts}] `{event.name}`: {one_line(clean_args(event.args), 90)}" for event in tools[-12:]]
        sections.append(("## 最近动作", "\n".join(recent)))
    last = thread.last_assistant()
    if last:
        sections.append(("## 当前状态", _clip_text(last, 500)))
    files = _files_touched(thread)
    if files:
        sections.append(("## 涉及文件", "\n".join(f"- {name}" for name in files)))

    for title, text in sections:
        used = len("\n".join(body))
        room = budget - used - len(title) - 8
        if room < 120:
            break
        body.extend([title, "", _clip_text(text, room), ""])

    return _fit_bytes("\n".join(body).rstrip() + "\n", budget)
