"""Keep an editable task beside sealed packets and remember each host's handoff."""
from __future__ import annotations

import json
import copy
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import bridge, focus, redact, sessions, state, workspace
from .model import Thread


def _active_path(root: Path, host: str) -> Path:
    bridge._host(host)
    return Path(root) / "active" / (host + ".json")


def _draft(root: Path, host: str, identifier: str) -> Path:
    bridge._path(root, identifier)  # Validate before constructing another path.
    base = (Path(root) / "work" / host).resolve()
    directory = (base / identifier).resolve()
    if directory.parent != base:
        raise ValueError("任务草稿路径越界")
    return directory / "STATE.json"


def _json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} 必须是 JSON 对象")
    return data


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".w-" + uuid.uuid4().hex[:20] + ".json")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _lock(root: Path, host: str):
    path = _active_path(root, host).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir()
    except FileExistsError as error:
        raise ValueError("本端任务正在更新或上次操作中断；先检查 active 下的锁目录。") from error
    try:
        yield
    finally:
        path.rmdir()


def _load(root: Path, host: str) -> dict | None:
    path = _active_path(root, host)
    if not path.exists():
        return None
    active = _json(path)
    if active.get("schema") != 1 or active.get("host") != host or active.get("phase") not in ("active", "returned"):
        raise ValueError("当前任务指针不合规")
    if "receiver_thread" in active and (not isinstance(active["receiver_thread"], str) or not active["receiver_thread"]):
        raise ValueError("当前任务绑定的会话ID不合规")
    if "project" in active and not isinstance(active["project"], str):
        raise ValueError("当前任务的工程目录不合规")
    checked = bridge.inspect(root, active.get("transfer", ""))
    transfer = checked["transfer"]
    if transfer["to"] != host or active.get("seal") != transfer["seal"] or checked["receipt"] is None:
        raise ValueError("当前任务与已接收交接不匹配")
    if active["phase"] == "returned":
        back = bridge.inspect(root, active.get("return", ""))["transfer"]
        if back["reply_to"] != transfer["id"] or back["from"] != host or back["to"] != transfer["from"] or back["chain"] != transfer["chain"]:
            raise ValueError("当前任务的返回链不匹配")
    return active


def _view(root: Path, host: str, active: dict) -> dict:
    checked = bridge.inspect(root, active["transfer"])
    transfer = checked["transfer"]
    draft = _draft(root, host, active["transfer"])
    problems = state.check(draft)
    data = _json(draft) if not problems else None
    material = ""
    if data is not None:
        try:
            source = checked["manifest"]
            thread = Thread(source["host"], source["thread"], events=source["_events"])
            selectors = data.get("context", {})
            own = {"host": host, "thread": active.get("receiver_thread")}
            live = bool(own["thread"] and (selectors.get("refs") or selectors.get("since"))
                        and selectors.get("origin", own) == own)
            if live:
                thread = copy.deepcopy(sessions.resolve(host, own["thread"]))
                redact.redact_thread(thread)
            material = focus.build(thread, data, checked["project"], live=live)
        except (ValueError, OSError, RuntimeError) as error:
            problems.append(str(error))
    freshness = workspace.compare(checked["project"], active.get("project"))
    return {"host": host, "id": active["transfer"], "phase": active["phase"],
            "from": transfer["from"], "chain": transfer["chain"], "return": active.get("return"),
            "workspace": transfer["workspace"], "state_path": str(draft),
            "packet_path": str(checked["path"] / "packet"), "state": data,
            "receiver_thread": active.get("receiver_thread"), "project": checked["project"],
            "freshness": freshness, "focus": material,
            "state_problems": problems, "source_resume": checked["manifest"]["_resume"]}


def current(root: Path, host: str) -> dict | None:
    """Read only; an invalid editable state is reported without losing its path."""
    active = _load(root, host)
    return _view(root, host, active) if active else None


def bound_thread(root: Path, host: str) -> str:
    active = _load(root, host)
    if active is None or not active.get("receiver_thread"):
        raise ValueError("当前任务尚未绑定本端会话；take --thread 绑定，或 reply --thread 指定。")
    return active["receiver_thread"]


def take(root: Path, host: str, identifier: str = "latest", switch: bool = False,
         thread=None, project: str | Path | None = None) -> dict:
    if thread is not None and thread.host != host:
        raise ValueError("接手绑定的会话不属于本端宿主")
    with _lock(root, host):
        active = _load(root, host)
        completed = bool(active and not state.check(_draft(root, host, active["transfer"]))
                         and _json(_draft(root, host, active["transfer"])).get("status") == "done")
        if identifier == "latest" and active and active["phase"] == "active" and not completed:
            identifier = active["transfer"]
        if identifier == "latest":
            pending = bridge.inbox(root, host)
            if not pending:
                if active and active["phase"] == "active" and completed:
                    return _view(root, host, active)
                raise ValueError(f"{host} 没有待接收交接；task 可查看上次任务，已接收包可按 ID 接手。")
            identifier = pending[0]["id"]
        if active and active["phase"] == "active" and not completed and active["transfer"] != identifier and not switch:
            raise ValueError("本端还有未返回的当前任务；先 reply，或显式使用 take <ID> --switch。")
        if active and active["phase"] == "returned" and active["transfer"] == identifier:
            raise ValueError("此任务已经返回；请用 task 或 receive 按 ID 查看。")
        same = active and active["transfer"] == identifier
        if same and thread is not None and active.get("receiver_thread") not in (None, thread.thread_id) and not switch:
            raise ValueError("任务已绑定另一会话；确认续接后用 take --switch --thread 显式重新绑定。")
        checked = bridge.inspect(root, identifier)
        transfer = checked["transfer"]
        if transfer["to"] != host:
            raise ValueError(f"此交接发给 {transfer['to']}，不能由 {host} 接手")
        previous_returns = [item for item in bridge.inbox(root, transfer["from"], include_received=True)
                            if item["from"] == host and item["reply_to"] == identifier]
        if previous_returns:
            raise ValueError("此交接已有返回包；请用 receive 按 ID 查看历史，不重复接手旧任务。")
        draft = _draft(root, host, identifier)
        # Prepare a draft first; if acknowledgment fails, an explicit retry reuses it.
        if not draft.exists():
            _save(draft, json.loads(checked["manifest"]["_state"]))
        bridge.receive(root, host, identifier)
        active = {**(active if same else {}), "schema": 1, "host": host, "transfer": identifier, "seal": transfer["seal"], "phase": "active"}
        if thread is not None:
            active["receiver_thread"] = thread.thread_id
        if project is not None:
            active["project"] = str(Path(project).expanduser().resolve())
        _save(_active_path(root, host), active)
        return _view(root, host, active)


def reply(root: Path, host: str, thread, state_path: Path | None = None, note: str = "") -> dict:
    if thread.host != host:
        raise ValueError("返回会话所属宿主不匹配")
    with _lock(root, host):
        active = _load(root, host)
        if active is None:
            raise ValueError("没有当前任务；先运行 take --host " + host)
        if active.get("receiver_thread") not in (None, thread.thread_id):
            raise ValueError("当前任务绑定另一会话，不能静默导出本次传入的会话。")
        parent = bridge.inspect(root, active["transfer"])["transfer"]
        # Recover a publish that completed before the active pointer was saved.
        existing = [item for item in bridge.inbox(root, parent["from"], include_received=True)
                    if item["from"] == host and item["reply_to"] == active["transfer"]]
        if len(existing) > 1:
            raise ValueError("原交接存在多个返回包；请按 ID 核对，不再自动重复发送。")
        if active["phase"] == "returned" or existing:
            identifier = active.get("return") or existing[0]["id"]
            sent = bridge.inspect(root, identifier)["transfer"]
            reused = True
        else:
            draft = Path(state_path) if state_path else _draft(root, host, active["transfer"])
            problems = state.check(draft)
            if problems:
                raise ValueError("请先修正可编辑状态：" + "；".join(problems))
            data = focus.normalize(thread, _json(draft), prior_packet=active["transfer"])
            sent = bridge.send(root, thread, parent["from"], data, active["transfer"], note,
                               project=active.get("project"))
            reused = False
        active.update(phase="returned", **{"return": sent["id"]})
        _save(_active_path(root, host), active)
        return {"id": sent["id"], "from": host, "to": parent["from"], "chain": parent["chain"],
                "reply_to": active["transfer"], "phase": "returned", "reused": reused,
                "path": str(bridge._path(root, sent["id"])),
                "receiver_prompt": bridge.receiver_prompt(root, sent["id"], parent["from"]),
                "note": "已存在返回包，未再次发送草稿；如草稿有新修改，请显式 handoff。" if reused else ""}
