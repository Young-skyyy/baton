"""A local two-host mailbox: sealed packets, explicit receipts, and linked returns.

This transfers task data. It never edits native session logs or starts either
host, and receiving a packet does not execute the historical commands in it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import bundle, focus, redact, render, state, workspace
from .model import Thread

HOSTS = ("codex", "dsh")
PACKET_FILES = {"MANIFEST.json", "events.jsonl", "THREAD.md", "BRIEF.md", "RESUME.md", "STATE.json", "ENV.md", "PROJECT.json", "FOCUS.md"}
ID_PATTERN = re.compile(r"handoff-[0-9a-f]{20}\Z")


def default_root() -> Path:
    configured = os.environ.get("BATON_HOME")
    if configured:
        return Path(configured).expanduser()
    workspace = Path(__file__).resolve().parents[3]
    shared = workspace / ".memory"
    return shared / "handoffs" if shared.is_dir() else Path.home() / ".baton" / "handoffs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _host(value: str) -> None:
    if value not in HOSTS:
        raise ValueError("host 必须是 codex 或 dsh")


def _path(root: Path, transfer_id: str) -> Path:
    if not ID_PATTERN.fullmatch(transfer_id):
        raise ValueError("交接 ID 格式不正确")
    items = (Path(root) / "items").resolve()
    path = (items / transfer_id).resolve()
    if path.parent != items:
        raise ValueError("交接路径越出收件箱")
    return path


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 必须是 JSON 对象")
    return value


def _digest(value: dict) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def inspect(root: Path, transfer_id: str) -> dict:
    path = _path(root, transfer_id)
    transfer = _read_json(path / "TRANSFER.json")
    if transfer.get("schema") != 1 or transfer.get("id") != transfer_id:
        raise ValueError("交接清单版本或 ID 不匹配")
    _host(transfer.get("from"))
    _host(transfer.get("to"))
    if transfer["from"] == transfer["to"]:
        raise ValueError("交接两端不能相同")
    seal = transfer.get("seal")
    body = {key: value for key, value in transfer.items() if key != "seal"}
    if seal != _digest(body):
        raise ValueError("交接清单已改变")
    hashes = transfer.get("files")
    if not isinstance(hashes, dict) or not {"MANIFEST.json", "events.jsonl", "THREAD.md", "RESUME.md", "STATE.json"}.issubset(hashes):
        raise ValueError("交接包缺少必需文件")
    for name, expected in hashes.items():
        if name not in PACKET_FILES:
            raise ValueError("交接包包含不支持的文件引用")
        file = path / "packet" / name
        if file.resolve().parent != (path / "packet").resolve():
            raise ValueError("交接文件路径越界")
        if hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError(f"交接文件已改变：{name}")
    manifest = bundle.read(path / "packet")
    if manifest.get("host") != transfer["from"] or manifest.get("thread") != transfer.get("source_thread"):
        raise ValueError("来源会话与交接清单不一致")
    problems = state.check(path / "packet" / "STATE.json")
    if problems:
        raise ValueError("交接状态不合规：" + "；".join(problems))
    receipt = None
    if (path / "RECEIPT.json").exists():
        receipt = _read_json(path / "RECEIPT.json")
        if receipt.get("id") != transfer_id or receipt.get("host") != transfer["to"] or receipt.get("seal") != seal or receipt.get("status") != "received":
            raise ValueError("接收回执与交接不匹配")
    project = _read_json(path / "packet" / "PROJECT.json") if "PROJECT.json" in hashes else None
    if project is not None:
        workspace.validate(project)
    focused = (path / "packet" / "FOCUS.md").read_text(encoding="utf-8") if "FOCUS.md" in hashes else ""
    return {"transfer": transfer, "receipt": receipt, "path": path, "manifest": manifest,
            "project": project, "focus": focused}


def receiver_prompt(root: Path, identifier: str, host: str) -> str:
    launcher = Path(__file__).resolve().parents[2] / "baton.ps1"
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    return (f"接手发给 {host} 的 baton 任务 {identifier}。由你确定本端当前真实会话ID，执行：\n"
            f"powershell -NoProfile -ExecutionPolicy Bypass -File {quote(launcher)} take {identifier} "
            f"--host {host} --root {quote(Path(root).resolve())} --thread <由你填写本次真实会话ID>\n"
            "先读当前状态、焦点材料和工程核对结果，按用户当前授权继续；工程已变化时先复核。"
            "更新可编辑状态后执行 reply --host " + host + "，已绑定的会话无需再填ID。")


def send(root: Path, thread: Thread, target: str, state_data: dict | None = None,
         reply_to: str | None = None, note: str = "", project: str | Path | None = None) -> dict:
    _host(thread.host)
    _host(target)
    if thread.host == target:
        raise ValueError("交接目标必须是另一端")
    if thread.meta.get("thread_source") in ("guardian_review", "subagent"):
        raise ValueError("请选择真实用户会话，不能交接审查或子代理会话")
    parent = inspect(root, reply_to) if reply_to else None
    if parent:
        previous = parent["transfer"]
        if previous["to"] != thread.host or previous["from"] != target:
            raise ValueError("返回方向与原交接不一致")
        if parent["receipt"] is None:
            raise ValueError("原交接尚未接收，不能作为返回链路")
    snapshot = copy.deepcopy(thread)
    hits = redact.redact_thread(snapshot)
    state_data = copy.deepcopy(state_data) if state_data is not None else state.skeleton(snapshot, render.assess(snapshot))
    problems = state.problems(state_data)
    if problems:
        raise ValueError("请先填写有效状态：" + "；".join(problems))
    state_data = focus.normalize(snapshot, state_data)
    project = project or state_data.get("project") or state_data.get("evidence", {}).get("project")
    if project:
        state_data["project"] = str(Path(project).expanduser().resolve())
    elif state_data.get("context", {}).get("files"):
        raise ValueError("指定相关文件时请同时指定 --project 或 STATE.project")
    project_data = workspace.capture(project, state_data.get("context", {}).get("files", []), state_data.get("checks", [])) if project else None
    # Reuse the recursive scanner for manually written state and the transfer note.
    from .model import Event
    carrier = Thread(snapshot.host, snapshot.thread_id, events=[Event(1, "", "meta", text=note, raw={"state": state_data, "project": project_data})])
    redact.redact_thread(carrier)
    state_data, project_data, note = carrier.events[0].raw["state"], carrier.events[0].raw["project"], carrier.events[0].text
    focused = focus.build(snapshot, state_data, project_data)
    root = Path(root)
    items = root / "items"
    items.mkdir(parents=True, exist_ok=True)
    transfer_id = "handoff-" + uuid.uuid4().hex[:20]
    destination = _path(root, transfer_id)
    # Shared mailbox directories must inherit the parent workspace access.
    # Windows Python 3.12.10+ treats mkdtemp mode 0700 as owner-only ACLs.
    staging = items / (".p-" + uuid.uuid4().hex[:20])
    staging.mkdir()
    try:
        packet = staging / "packet"
        bundle.write(snapshot, packet, render.l1(snapshot), render.brief(snapshot), render.resume(snapshot, state_data=state_data))
        state.write(packet / "STATE.json", state_data)
        (packet / "FOCUS.md").write_text(focused, encoding="utf-8")
        if project_data is not None:
            (packet / "PROJECT.json").write_text(json.dumps(project_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        problems = state.check(packet / "STATE.json")
        if problems:
            raise ValueError("请先填写有效状态：" + "；".join(problems))
        transfer = {"schema": 1, "id": transfer_id, "from": snapshot.host, "to": target,
                    "source_thread": snapshot.thread_id, "workspace": snapshot.cwd, "created": _now(),
                    "chain": parent["transfer"]["chain"] if parent else transfer_id, "reply_to": reply_to,
                    "task": state_data["task"], "note": note,
                    "files": {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in packet.iterdir() if file.is_file()}}
        transfer["seal"] = _digest(transfer)
        (staging / "TRANSFER.json").write_text(json.dumps(transfer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.rename(staging, destination)
    finally:
        if staging.exists():
            if staging.resolve().parent != items.resolve():
                raise ValueError("临时交接目录越界，拒绝清理")
            shutil.rmtree(staging)
    checked = inspect(root, transfer_id)
    return {"id": transfer_id, "from": snapshot.host, "to": target, "task": state_data["task"],
            "path": str(checked["path"]), "chain": transfer["chain"], "reply_to": reply_to,
            "status": "pending", "redacted_fields": sum(hits.values()),
            "receiver_prompt": receiver_prompt(root, transfer_id, target)}


def inbox(root: Path, host: str, include_received: bool = False) -> list:
    _host(host)
    found = []
    items = Path(root) / "items"
    if not items.exists():
        return found
    for path in items.iterdir():
        if not path.is_dir() or not ID_PATTERN.fullmatch(path.name):
            continue
        checked = inspect(root, path.name)
        transfer = checked["transfer"]
        if transfer["to"] != host or (checked["receipt"] is not None and not include_received):
            continue
        found.append({"id": transfer["id"], "from": transfer["from"], "to": host,
                      "task": transfer["task"], "created": transfer["created"],
                      "source_thread": transfer["source_thread"], "chain": transfer["chain"],
                      "reply_to": transfer["reply_to"], "path": str(checked["path"]),
                      "status": "received" if checked["receipt"] else "pending"})
    return sorted(found, key=lambda item: (item["created"], item["id"]), reverse=True)


def receive(root: Path, host: str, transfer_id: str = "latest", note: str = "") -> dict:
    _host(host)
    if transfer_id == "latest":
        pending = inbox(root, host)
        if not pending:
            raise ValueError(f"{host} 没有待接收的交接；已接收的请按 ID 重读")
        transfer_id = pending[0]["id"]
    checked = inspect(root, transfer_id)
    transfer = checked["transfer"]
    if transfer["to"] != host:
        raise ValueError(f"此交接发给 {transfer['to']}，不能由 {host} 接收")
    receipt = checked["receipt"]
    if receipt is None:
        receipt = {"schema": 1, "id": transfer_id, "host": host, "status": "received",
                   "received": _now(), "seal": transfer["seal"], "note": redact.redact_text(note)[0]}
        path = checked["path"] / "RECEIPT.json"
        lock = checked["path"] / ".receipt-lock"
        try:
            lock.mkdir()
        except FileExistsError as error:
            raise ValueError("此交接正在接收或上次操作中断；先检查 .receipt-lock，再重试。") from error
        temporary = path.with_name(".r-" + uuid.uuid4().hex[:20] + ".json")
        try:
            if path.exists():
                receipt = inspect(root, transfer_id)["receipt"]
            else:
                temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                os.replace(temporary, path)  # Atomic publish while this receipt's writer lock is held.
        finally:
            temporary.unlink(missing_ok=True)
            lock.rmdir()
    packet = checked["path"] / "packet"
    return {"id": transfer_id, "from": transfer["from"], "to": host, "chain": transfer["chain"],
            "reply_to": transfer["reply_to"], "path": str(packet), "workspace": transfer["workspace"],
            "note": transfer["note"], "receipt": receipt,
            "state": json.loads(checked["manifest"]["_state"]), "resume": checked["manifest"]["_resume"],
            "project": checked["project"], "focus": checked["focus"]}
