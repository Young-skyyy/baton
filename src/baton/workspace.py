"""Bounded, read-only project fingerprints. No historical command is executed."""
from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MAX_FILES = 64
MAX_BYTES = 2 * 1024 * 1024


def relative(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if not value or path.is_absolute() or ".." in path.parts or ":" in str(path) or str(path) == ".":
        raise ValueError("工程文件必须是项目内的相对路径：" + value)
    return path.as_posix()


def _git(project: Path, *args) -> tuple[str, str]:
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(project), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)
    except (OSError, subprocess.SubprocessError) as error:
        return "", str(error)[:200]
    if result.returncode:
        return "", result.stderr.decode("utf-8", "replace").strip()[:200]
    return result.stdout.decode("utf-8", "replace"), ""


def _file(project: Path, name: str) -> dict:
    name = relative(name)
    item = {"path": name}
    path = project / name
    lower = path.name.lower()
    if lower.startswith(".env") or lower in ("credentials", "credentials.json", "secrets.json") or path.suffix.lower() in (".pem", ".key", ".pfx", ".p12"):
        return {**item, "status": "excluded_sensitive"}
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(project):
            return {**item, "status": "excluded_link"}
        if not path.exists():
            return {**item, "status": "missing"}
        if not path.is_file():
            return {**item, "status": "not_file"}
        with path.open("rb") as handle:
            body = handle.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            return {**item, "status": "too_large"}
        return {**item, "status": "hashed", "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    except OSError as error:
        return {**item, "status": "unreadable", "error": type(error).__name__}


def capture(project: str | Path, files=(), checks=()) -> dict:
    if not isinstance(project, (str, Path)):
        raise ValueError("工程目录必须是路径")
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise ValueError("工程目录不存在：" + str(project))
    explicit = list(dict.fromkeys(relative(name) for name in files))
    head, head_error = _git(project, "rev-parse", "HEAD")
    branch, branch_error = _git(project, "symbolic-ref", "--quiet", "--short", "HEAD")
    status, status_error = _git(project, "status", "--porcelain=v1", "--untracked-files=all", "-z")
    dirty = []
    fields = iter(status.split("\0"))
    for field in fields:
        if not field:
            continue
        entry = {"status": field[:2], "path": field[3:]}
        if "R" in field[:2] or "C" in field[:2]:
            entry["from_path"] = next(fields, "")
        dirty.append(entry)
    names = dict.fromkeys(explicit)
    for entry in dirty:
        for value in (entry["path"], entry.get("from_path")):
            if value:
                names[value] = None
    tracked = [_file(project, name) for name in list(names)[:MAX_FILES]]
    reports = []
    for value in checks:
        report = {"command": value["command"], "exit_code": value.get("exit_code"),
                  "observed_at": value.get("observed_at"), "verification": "sender_reported"}
        if value.get("log"):
            report["log"] = _file(project, value["log"])
        reports.append(report)
    return {"schema": 1, "project": str(project), "captured": datetime.now(timezone.utc).isoformat(),
            "git": {"available": not head_error and not status_error, "head": head.strip(),
                    "branch": branch.strip() if not branch_error else "(detached or unavailable)",
                    "dirty": dirty[:MAX_FILES], "dirty_count": len(dirty),
                    "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
                    "errors": [error for error in (head_error, status_error) if error]},
            "files": tracked, "omitted_files": max(0, len(names) - MAX_FILES), "checks": reports}


def validate(value: dict) -> None:
    if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("project"), str):
        raise ValueError("工程快照格式不正确")
    if not isinstance(value.get("git"), dict) or not isinstance(value.get("files"), list) or len(value["files"]) > MAX_FILES:
        raise ValueError("工程快照缺少版本与文件范围")
    if not isinstance(value["git"].get("available"), bool) or not isinstance(value["git"].get("dirty"), list):
        raise ValueError("工程快照Git状态不合规")
    if any(not isinstance(value["git"].get(key), str) for key in ("head", "branch", "status_sha256")):
        raise ValueError("工程快照版本标识不合规")
    if not isinstance(value.get("checks", []), list) or len(value.get("checks", [])) > 32:
        raise ValueError("工程快照测试记录不合规")
    for item in value["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("工程快照文件条目不合规")
        relative(item["path"])
    for report in value.get("checks", []):
        if not isinstance(report, dict) or not isinstance(report.get("command"), str):
            raise ValueError("工程快照测试条目不合规")
        if report.get("log"):
            if not isinstance(report["log"], dict) or not isinstance(report["log"].get("path"), str):
                raise ValueError("工程快照测试日志不合规")
            relative(report["log"]["path"])


def compare(saved: dict | None, project: str | Path | None = None) -> dict:
    if saved is None:
        return {"status": "not_recorded", "changes": [], "note": "此包未记录工程快照"}
    validate(saved)
    target = Path(project or saved["project"]).expanduser().resolve()
    if not target.is_dir():
        return {"status": "missing", "project": str(target), "changes": [], "note": "当前机器找不到此工程；用 take --project 指定目录"}
    now = capture(target, [item["path"] for item in saved["files"]])
    changes = []
    if saved["git"].get("available") and now["git"]["available"]:
        for key in ("head", "branch", "status_sha256"):
            if saved["git"].get(key) != now["git"][key]:
                changes.append({"kind": "git", "field": key})
    current = {item["path"]: item for item in now["files"]}
    partial = bool(saved.get("omitted_files") or now.get("omitted_files"))
    partial = partial or not saved["git"].get("available") or not now["git"]["available"]
    for before in saved["files"]:
        after = current.get(before["path"], {"status": "not_checked"})
        known = {"hashed", "missing"}
        if before.get("status") not in known or after.get("status") not in known:
            partial = True
        elif (before.get("status"), before.get("sha256")) != (after.get("status"), after.get("sha256")):
            changes.append({"kind": "file", "path": before["path"], "before": before.get("status"), "after": after.get("status")})
    # A test log fingerprint is evidence about the log, not proof tests ran on this tree.
    for report in saved.get("checks", []):
        before = report.get("log")
        if isinstance(before, dict) and before.get("status") == "hashed":
            after = _file(target, before["path"])
            if after.get("sha256") != before.get("sha256"):
                changes.append({"kind": "test_log", "path": before["path"]})
    return {"status": "changed" if changes else "partial" if partial else "matched", "project": str(target),
            "mapped": str(target) != saved["project"], "changes": changes,
            "note": "只核对已记录范围；测试结论仍需按证据复核", "checked_files": len(saved["files"])}


def render(saved: dict | None, freshness: dict | None = None) -> str:
    if saved is None:
        return "工程：未记录快照"
    labels = {"matched": "已记录范围一致", "changed": "工程已变化，先复核再继续", "partial": "部分范围无法核对",
              "missing": "工程目录不存在", "not_recorded": "未记录"}
    lines = [f"工程：{saved['project']}", f"版本：{saved['git'].get('branch', '')} / {saved['git'].get('head', '')[:12]}",
             f"未提交条目：{saved['git'].get('dirty_count', 0)}；文件指纹：{len(saved['files'])}；省略：{saved.get('omitted_files', 0)}"]
    if freshness:
        lines.append("接手核对：" + labels.get(freshness["status"], freshness["status"]))
        for change in freshness["changes"][:12]:
            lines.append("- " + change.get("path", "git " + change.get("field", "")))
    if saved.get("checks"):
        lines.append("测试记录（交接方填报，不自动执行）：")
        lines.extend(f"- {item['command']}；exit={item.get('exit_code')}；时间={item.get('observed_at') or '未填'}" for item in saved["checks"])
    return "\n".join(lines)
