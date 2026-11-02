"""Environment snapshot: what the next agent cannot see from the transcript alone."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".dsh", ".codex", ".cache"}
MAX_FILES = 8
MAX_AGE_SECONDS = 24 * 3600


def _run(args: list, cwd: str, timeout: int = 5) -> str:
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git(cwd: str) -> dict:
    if not (Path(cwd) / ".git").exists():
        return {}
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    status = _run(["git", "status", "--porcelain"], cwd)
    last = _run(["git", "log", "-1", "--oneline"], cwd)
    return {
        "branch": branch,
        "dirty": [line for line in status.splitlines() if line.strip()],
        "last_commit": last,
    }


def _recent_files(cwd: str) -> list:
    now = time.time()
    found = []
    base = Path(cwd)
    if not base.is_dir():
        return found
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        depth = len(Path(root).relative_to(base).parts)
        if depth >= 3:
            dirs[:] = []
        for name in files:
            path = Path(root) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            if now - stat.st_mtime > MAX_AGE_SECONDS:
                continue
            found.append((stat.st_mtime, str(path.relative_to(base))))
    found.sort(reverse=True)
    return [{"path": path, "mtime": time.strftime("%m-%d %H:%M", time.localtime(ts))} for ts, path in found[:MAX_FILES]]


def _processes(cwd: str) -> list:
    if sys.platform != "win32":
        return []
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*" + cwd.replace("'", "''") + "*' } "
        "| Select-Object ProcessId,Name | ConvertTo-Json -Compress"
    )
    raw = _run(["powershell", "-NoProfile", "-Command", script], cwd, timeout=8)
    if not raw:
        return []
    import json

    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [{"pid": item.get("ProcessId"), "name": item.get("Name")} for item in data if isinstance(item, dict)]


def capture(cwd: str) -> dict:
    if not cwd or not Path(cwd).is_dir():
        return {}
    return {
        "cwd": cwd,
        "git": _git(cwd),
        "recent_files": _recent_files(cwd),
        "processes": _processes(cwd),
    }


def render(snap: dict) -> str:
    if not snap:
        return "# 环境快照\n\n（未能捕获）\n"
    lines = ["# 环境快照", "", f"- 目录: `{snap.get('cwd')}`"]
    git = snap.get("git") or {}
    if git:
        lines.append(f"- 分支: `{git.get('branch') or '?'}`，未提交 {len(git.get('dirty') or [])} 项")
        if git.get("last_commit"):
            lines.append(f"- 最近提交: `{git['last_commit']}`")
        for item in (git.get("dirty") or [])[:10]:
            lines.append(f"  - `{item}`")
    files = snap.get("recent_files") or []
    if files:
        lines.append("")
        lines.append("## 24 小时内改过")
        lines.extend(f"- `{item['path']}` ({item['mtime']})" for item in files)
    procs = snap.get("processes") or []
    if procs:
        lines.append("")
        lines.append("## 目录相关的进程")
        lines.extend(f"- {item['name']} (pid {item['pid']})" for item in procs[:5])
    lines.append("")
    lines.append("> 快照是**导出那一刻**的状态；接手时可能已经变了，先核对再动手。")
    return "\n".join(lines) + "\n"


def summary(snap: dict) -> list:
    if not snap:
        return []
    out = []
    git = snap.get("git") or {}
    if git:
        out.append(f"分支 `{git.get('branch') or '?'}`，未提交 {len(git.get('dirty') or [])} 项")
    files = snap.get("recent_files") or []
    if files:
        names = "、".join(f"`{item['path']}`" for item in files[:3])
        out.append(f"最近改动 {len(files)} 个文件：{names}")
    procs = snap.get("processes") or []
    if procs:
        out.append("相关进程：" + "、".join(f"{item['name']}({item['pid']})" for item in procs[:3]))
    return out
