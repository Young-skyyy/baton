"""Narrow conflict detection: two opposite assertions about the same thing.

Deliberately conservative. A sentence keeps a key built by deleting its
polarity word; two statements conflict only when the keys share a prefix of at
least four characters and the polarity is opposite. A miss is acceptable, a
false alarm is not — the receiver acts on what we print.
"""

from __future__ import annotations

import re

from .model import Thread, one_line

PAIRS = [
    ("不需要", "需要"),
    ("不建议", "建议"),
    ("不可以", "可以"),
    ("不支持", "支持"),
    ("不用", "用"),
    ("不能", "能"),
    ("不是", "是"),
]
SENTENCE = re.compile(r"[。！？\n；;]")
MIN_PREFIX = 4


def _key_and_polarity(sentence: str) -> tuple:
    for negative, positive in PAIRS:
        if negative in sentence:
            return sentence.replace(negative, ""), "neg"
        if positive in sentence:
            return sentence.replace(positive, ""), "pos"
    return "", ""


def _normalize(text: str) -> str:
    return re.sub(r"[\s，,。！？；;：:`*_\"'()（）\[\]-]", "", text)


def _related(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    common = 0
    for left, right in zip(a, b):
        if left != right:
            break
        common += 1
    return common >= MIN_PREFIX


def detect(thread: Thread, limit: int = 2) -> list:
    seen = []
    conflicts = []
    for event in thread.events:
        if event.kind not in ("assistant", "user"):
            continue
        for raw in SENTENCE.split(event.text or ""):
            sentence = raw.strip()
            if len(sentence) < 6:
                continue
            key_raw, polarity = _key_and_polarity(sentence)
            key = _normalize(key_raw)
            if not key or len(key) < 3:
                continue
            current = {"seq": event.seq, "polarity": polarity, "text": one_line(sentence, 70)}
            clash = None
            for previous in seen:
                if previous["polarity"] != polarity and _related(previous["key"], key):
                    clash = previous
                    break
            if clash:
                conflicts.append({"topic": clash["key"][:20], "a": clash, "b": current})
                seen.remove(clash)
                continue
            seen.append({**current, "key": key})
    return conflicts[-limit:]
