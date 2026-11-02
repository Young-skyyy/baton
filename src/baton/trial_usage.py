"""Extract measured receiver usage from a local Codex rollout, without exporting its content."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")


def _usage(value):
    if not isinstance(value, dict):
        return None
    if any(type(value.get(key)) is not int or value[key] < 0 for key in ("input_tokens", "output_tokens", "total_tokens")):
        return None
    if value["input_tokens"] + value["output_tokens"] != value["total_tokens"]:
        return None
    result = {key: value.get(key) if type(value.get(key)) is int and value[key] >= 0 else None for key in FIELDS}
    if result["cached_input_tokens"] is not None and result["cached_input_tokens"] > result["input_tokens"]:
        return None
    return result


def _elapsed(start, end):
    try:
        seconds = (datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
        return seconds if seconds >= 0 else None
    except (ValueError, TypeError, AttributeError):
        return None


def inspect_rollout(path: Path) -> dict:
    """Use unique response records, cross-check their sum, and keep unavailable values null.

    Input includes host/system prompts and repeated tool-round context. Cached and
    reasoning counters are subsets, not additional tokens to add to total_tokens.
    """
    responses = {}
    cumulative = None
    models = set()
    agent_path = None
    started = ended = None
    starts = completions = calls = malformed = 0
    invalid_native = False
    last_event = None
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(row, dict):
                malformed += 1
                continue
            kind = row.get("type")
            data = row.get("payload")
            if not isinstance(data, dict):
                continue
            if kind == "session_meta":
                agent_path = data.get("agent_path") if isinstance(data.get("agent_path"), str) else None
            elif kind == "turn_context" and isinstance(data.get("model"), str):
                models.add(data["model"])
            elif kind == "response_item" and data.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
                calls += 1
            elif kind == "token_usage_record":
                usage = _usage(data.get("usage"))
                response_id = data.get("response_id")
                if usage is None or not isinstance(response_id, str) or not response_id:
                    invalid_native = True
                else:
                    responses[response_id] = usage
            elif kind == "event_msg":
                event = data.get("type")
                last_event = event
                if event == "task_started":
                    starts += 1
                    started = started or row.get("timestamp")
                elif event == "task_complete":
                    completions += 1
                    ended = row.get("timestamp")
                elif event == "token_count":
                    info = data.get("info")
                    cumulative = _usage(info.get("total_token_usage")) if isinstance(info, dict) else None
    usage = None
    method = None
    consistent = None
    if responses and not invalid_native:
        usage = {}
        for field in FIELDS:
            values = [value[field] for value in responses.values()]
            usage[field] = sum(values) if all(value is not None for value in values) else None
        method = "sum_unique_response_usage"
        if cumulative is not None:
            consistent = all(usage[field] == cumulative[field] for field in ("input_tokens", "output_tokens", "total_tokens"))
    elif not responses and not invalid_native and cumulative is not None:
        usage, method = cumulative, "last_reported_cumulative_usage"
    finalized = starts > 0 and starts == completions and last_event == "task_complete" and malformed == 0 and not invalid_native and consistent is not False and usage is not None
    return {
        "kind": "codex-receiver-usage", "agent_path": agent_path, "models": sorted(models),
        "task_count": starts, "completed_task_count": completions, "measurement_finalized": finalized,
        "method": method, "consistent_with_cumulative": consistent, "usage": usage,
        "unique_responses": len(responses), "model_tool_calls": calls, "malformed_records": malformed,
        "started_at": started, "ended_at": ended, "elapsed_seconds": _elapsed(started, ended),
        "scope": "整个接手会话的所有模型请求，包含宿主提示与工具往返中的重复上下文；不含主代理设计和评审开销，不代表交接包本身体积或人民币账单",
    }
