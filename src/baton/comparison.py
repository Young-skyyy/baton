"""Summarize matched, scored comparison cases; never infer correctness from words."""
from __future__ import annotations

import json
from pathlib import Path

CONDITIONS = ("full", "brief", "baton")


def _integer(value):
    return type(value) is int and value >= 0


def _usage(record):
    measured = record.get("measurement")
    if not isinstance(measured, dict) or measured.get("measurement_finalized") is not True:
        return None
    usage = measured.get("usage")
    if not isinstance(usage, dict) or not all(_integer(usage.get(key)) for key in ("input_tokens", "output_tokens", "total_tokens")):
        return None
    if usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]:
        return None
    return usage


def _saving(treatment, baseline):
    return 1 - treatment / baseline if baseline else None


def _models(record):
    measured = record.get("measurement")
    models = measured.get("models") if isinstance(measured, dict) else None
    if not isinstance(models, list) or not models or any(not isinstance(model, str) or not model for model in models):
        return None
    return tuple(sorted(set(models)))


def summarize(records: list) -> dict:
    if not isinstance(records, list) or not records:
        raise ValueError("records 必须是非空列表")
    cases = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("case_id"), str) or not record["case_id"]:
            raise ValueError("每条记录需要 case_id")
        condition = record.get("condition")
        if condition not in CONDITIONS:
            raise ValueError("condition 必须是 full/brief/baton")
        if "family" in record and (not isinstance(record["family"], str) or not record["family"]):
            raise ValueError("family 必须是非空字符串")
        case = cases.setdefault(record["case_id"], {})
        if condition in case:
            raise ValueError("同一案例和条件不能重复")
        judgements = record.get("judgements")
        if not isinstance(judgements, list) or not judgements:
            raise ValueError("每条记录需要非空 judgements")
        for item in judgements:
            if not isinstance(item, dict) or item.get("outcome") not in ("correct", "incorrect", "abstain", None):
                raise ValueError("outcome 必须是 correct/incorrect/abstain/null")
            if item.get("outcome") is not None and (not isinstance(item.get("evidence"), str) or not item["evidence"].strip()):
                raise ValueError("判分必须提供依据")
            if item.get("supported") is not None and type(item["supported"]) is not bool:
                raise ValueError("supported 必须是 true/false/null")
        case[condition] = record
    for case in cases.values():
        if set(case) != set(CONDITIONS):
            raise ValueError("每个案例必须有完整的三个条件，不能丢弃未通过的组")
        if len({len(record["judgements"]) for record in case.values()}) != 1:
            raise ValueError("同一案例的判分点数量必须一致")
        if len({record.get("family", "unspecified") for record in case.values()}) != 1:
            raise ValueError("同一案例的 family 必须一致")
        for field in ("questions", "source_sha256"):
            if any(field in record for record in case.values()):
                values = [record.get(field) for record in case.values()]
                if any(value is None for value in values) or any(value != values[0] for value in values[1:]):
                    raise ValueError(f"同一案例的 {field} 必须一致")
    return _group(cases)


def _group(cases, include_breakdown=True):
    conditions = {}
    measured_all = all(_usage(record) is not None for case in cases.values() for record in case.values())
    same_models = all(len({_models(record) for record in case.values()}) == 1 and _models(case["full"]) is not None for case in cases.values())
    for condition in CONDITIONS:
        rows = [case[condition] for case in cases.values()]
        points = [point for row in rows for point in row["judgements"]]
        complete = all(point.get("outcome") is not None for point in points)
        correct = sum(point.get("outcome") == "correct" for point in points)
        usage = [_usage(row) for row in rows]
        available = all(value is not None for value in usage)
        total = sum(value["total_tokens"] for value in usage) if available else None
        cached = [value.get("cached_input_tokens") for value in usage] if available else []
        conditions[condition] = {
            "cases": len(rows), "questions": len(points), "correct": correct,
            "incorrect": sum(point.get("outcome") == "incorrect" for point in points),
            "abstained": sum(point.get("outcome") == "abstain" for point in points),
            "unreviewed": sum(point.get("outcome") is None for point in points),
            "question_accuracy": correct / len(points) if complete else None,
            "fully_correct_cases": sum(all(point.get("outcome") == "correct" for point in row["judgements"]) for row in rows),
            "case_success_rate": sum(all(point.get("outcome") == "correct" for point in row["judgements"]) for row in rows) / len(rows) if complete else None,
            "grounded_accuracy": sum(point.get("outcome") == "correct" and point.get("supported") is True for point in points) / len(points)
                if complete and all(point.get("supported") is not None for point in points) else None,
            "receiver_total_tokens": total,
            "cached_input_tokens": sum(cached) if available and all(_integer(value) for value in cached) else None,
            "token_saving_vs_full": None,
        }
    if measured_all and same_models:
        baseline = conditions["full"]["receiver_total_tokens"]
        for condition in CONDITIONS:
            conditions[condition]["token_saving_vs_full"] = _saving(conditions[condition]["receiver_total_tokens"], baseline)
    result = {"kind": "paired-handoff-comparison", "matched_cases": len(cases), "conditions": conditions,
              "token_comparison_complete": measured_all and same_models,
              "ratio_definition": "1 - 条件下所有配对案例的接手会话total_tokens之和 / full条件对应之和；负值表示消耗增加。abstain计入问题总数但不算答对。"}
    if include_breakdown:
        families = sorted({case["full"].get("family", "unspecified") for case in cases.values()})
        result["by_family"] = {family: _group({key: case for key, case in cases.items() if case["full"].get("family", "unspecified") == family}, False) for family in families}
        result["by_case"] = {key: _group({key: case}, False) for key, case in cases.items()}
    return result


def load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("需要 schema=1 的对照文件")
    return summarize(data.get("records"))
