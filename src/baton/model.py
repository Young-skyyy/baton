"""Core data model: one thread, a linear stream of events, nothing else."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Event:
    seq: int
    ts: str
    kind: str
    text: str = ""
    name: str = ""
    args: str = ""
    result: str = ""
    call_id: str = ""
    ref: str = ""
    raw: object = None

    def to_dict(self) -> dict:
        out = {"seq": self.seq, "ts": self.ts, "kind": self.kind}
        for key in ("text", "name", "args", "result", "call_id", "ref"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.raw is not None:
            out["raw"] = self.raw
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        return cls(
            seq=int(data.get("seq", 0)),
            ts=str(data.get("ts", "")),
            kind=str(data.get("kind", "")),
            text=str(data.get("text", "")),
            name=str(data.get("name", "")),
            args=str(data.get("args", "")),
            result=str(data.get("result", "")),
            call_id=str(data.get("call_id", "")),
            ref=str(data.get("ref", "")),
            raw=data.get("raw"),
        )


@dataclass
class Thread:
    host: str
    thread_id: str
    cwd: str = ""
    source: str = ""
    events: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def counts(self) -> dict:
        out = {}
        for event in self.events:
            out[event.kind] = out.get(event.kind, 0) + 1
        return out

    def first_user(self) -> str:
        for event in self.events:
            if event.kind == "user":
                return event.text
        return ""

    def last_user(self) -> str:
        for event in reversed(self.events):
            if event.kind == "user":
                return event.text
        return ""

    def last_assistant(self) -> str:
        for event in reversed(self.events):
            if event.kind == "assistant":
                return event.text
        return ""

    def tool_names(self) -> list:
        return [event.name for event in self.events if event.kind == "tool_call" and event.name]

    def summary_line(self) -> str:
        counts = self.counts()
        return (
            f"{self.host}:{self.thread_id} "
            f"| {counts.get('user', 0)} 轮用户 / {counts.get('tool_call', 0)} 次工具 "
            f"| {one_line(self.first_user(), 60)}"
        )


def one_line(text: str, limit: int = 160) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


INJECTED_PREFIXES = (
    "<environment_context>",
    "<system-reminder>",
    "<user_instructions>",
    "<external_codex_apps_open_page>",
    "Current runtime context.",
    "Time sampled while preparing",
    "Instructions from:",
)


def is_injected(text: str) -> bool:
    """Harness-injected context arrives as a user message; it is not something the user said."""
    head = (text or "").lstrip()[:120]
    return head.startswith(INJECTED_PREFIXES)
