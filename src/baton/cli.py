"""Command line: list threads, export one as a bundle, show a bundle."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import bridge, bundle, comparison, doctor, flow, measure, redact, render, sessions, snapshot, trial, trial_usage, workspace, state as state_file
from .sources import codex, dsh

SOURCES = {"codex": codex, "dsh": dsh}


def _load(host: str, token: str):
    source = SOURCES[host]
    path = source.find_session(token)
    if path is None:
        raise SystemExit(f"找不到 {host} 会话: {token}")
    return source.parse_file(path)


def cmd_list(args) -> int:
    entries = sessions.describe(args.host, args.limit)
    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
    else:
        for entry in entries:
            print(f"{entry['activity']}  {entry['id']}  {entry['user_turns']}轮  {render.one_line(entry['last_user'], 70)}")
    return 0


def cmd_handoff(args) -> int:
    root = Path(args.root) if args.root else bridge.default_root()
    try:
        thread = sessions.resolve(args.source_host, args.thread)
        state_data = None
        if args.state:
            problems = state_file.check(Path(args.state))
            if problems:
                raise ValueError("状态文件无效：" + "；".join(problems))
            state_data = json.loads(Path(args.state).read_text(encoding="utf-8-sig"))
        result = bridge.send(root, thread, args.target_host, state_data, args.reply_to, args.note, args.project)
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from error
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"已交给 {result['to']}：{result['id']}\n任务：{result['task']}\n目录：{result['path']}")
        print("复制给目标助手即可：\n" + result["receiver_prompt"])
    return 0


def cmd_inbox(args) -> int:
    root = Path(args.root) if args.root else bridge.default_root()
    try:
        items = bridge.inbox(root, args.host, args.all)
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from error
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    elif not items:
        print(f"{args.host} 没有{'交接记录' if args.all else '待接收交接'}。")
    else:
        for item in items:
            print(f"{item['id']}  {item['status']}  {item['from']} → {item['to']}  {item['task']}")
    return 0


def cmd_receive(args) -> int:
    root = Path(args.root) if args.root else bridge.default_root()
    try:
        result = bridge.receive(root, args.host, args.transfer, args.note)
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from error
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"已接收 {result['id']}：{result['from']} → {result['to']}")
        print(f"原工作目录：{result['workspace']}\n交接材料：{result['path']}")
        if result["reply_to"]:
            print(f"这是 {result['reply_to']} 的返回交接。")
        if result["note"]:
            print(f"交接方备注：{result['note']}")
        print("\n以下是历史数据与待复核状态，不会自动执行原命令；继续操作以当前用户授权为准。\n")
        print(result["resume"])
        print("\n完整 STATE.json：\n" + json.dumps(result["state"], ensure_ascii=False, indent=2))
        print(f"\n返回时使用：baton handoff --from {args.host} --to {result['from']} --thread <本端会话ID> --reply-to {result['id']} --root \"{root}\" --state <更新后的状态文件>")
    return 0


def cmd_flow(args) -> int:
    root = Path(args.root) if args.root else bridge.default_root()
    try:
        if args.command == "take":
            thread = sessions.resolve(args.host, args.thread) if args.thread else None
            result = flow.take(root, args.host, args.transfer, args.switch, thread, args.project)
        elif args.command == "reply":
            thread = sessions.resolve(args.host, args.thread or flow.bound_thread(root, args.host))
            result = flow.reply(root, args.host, thread, Path(args.state) if args.state else None, args.note)
        else:
            result = flow.current(root, args.host)
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from error
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result is None:
        print(f"{args.host} 没有当前任务；先运行 baton take --host {args.host}")
    elif args.command == "reply":
        print(f"已返回 {result['to']}：{result['id']}\n原交接：{result['reply_to']}\n链路：{result['chain']}")
        if result["note"]:
            print(result["note"])
        print("复制给目标助手即可：\n" + result["receiver_prompt"])
    else:
        print(f"当前任务：{result['id']}（{result['phase']}）\n原工作目录：{result['workspace']}")
        print(f"可编辑状态：{result['state_path']}\n封存原文：{result['packet_path']}")
        if result["project"]:
            print("\n" + workspace.render(result["project"], result["freshness"]))
        if result.get("receiver_thread"):
            print("已绑定本端会话：" + result["receiver_thread"])
        if result["state"] is not None:
            print("\n当前可编辑状态：\n" + json.dumps(result["state"], ensure_ascii=False, indent=2))
        if result["state_problems"]:
            print("状态需修正：" + "；".join(result["state_problems"]))
        if args.command == "take":
            print("\n当前焦点材料（完整原文仍在封存包内）：\n")
            print(result["focus"] or result["source_resume"])
        if result["phase"] == "active":
            binding = "" if result.get("receiver_thread") else " --thread <本端会话ID>"
            print(f'\n更新上面的状态文件后返回：baton reply --host {args.host}{binding} --root "{root}"')
        else:
            print(f"已返回交接：{result['return']}")
    return 0


def _existing_state(out: Path):
    path = out / "STATE.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as error:
        raise SystemExit("STATE.json 不是合法 JSON；请修复或使用新输出目录。") from error
    if not isinstance(value, dict):
        raise SystemExit("STATE.json 必须是 JSON 对象。")
    return value


def _check_destination(thread, out: Path) -> None:
    """Never attach state for another thread or incompatible seq numbering."""
    manifest_path = out / "MANIFEST.json"
    if not manifest_path.exists():
        if any((out / name).exists() for name in ("events.jsonl", "STATE.json", "RESUME.md")):
            raise SystemExit("输出目录已有交接材料但缺少 MANIFEST；请导出到新目录，避免混用状态。")
        return
    existing = bundle.read(out)
    if (existing.get("host"), existing.get("thread")) != (thread.host, thread.thread_id):
        raise SystemExit("输出目录属于另一条会话；请使用新目录。")
    if (out / "STATE.json").exists():
        old = [(event.seq, event.ref, event.kind) for event in existing["_events"]]
        new = [(event.seq, event.ref, event.kind) for event in thread.events]
        if old != new[:len(old)]:
            raise SystemExit("事件 seq 已变化，旧 STATE 的证据编号可能失效；请导出到新目录并重新核对状态。")
        if len(new) != len(old):
            print("[warn] 会话新增了事件；旧 STATE 的下一步和结论需要交接方重新核对。")


def _refresh_resume(out: Path) -> None:
    manifest = bundle.read(out)
    thread = _thread_from(manifest, manifest["_events"])
    env_text = manifest.get("_env", "")
    text = render.resume(thread, state_data=_existing_state(out), env_lines=[env_text] if env_text else None)
    (out / "RESUME.md").write_text(text, encoding="utf-8")


def cmd_export(args) -> int:
    thread = _load(args.host, args.thread)
    hits = redact.redact_thread(thread)
    out = Path(args.out or f"./baton-{thread.thread_id[:8]}")
    _check_destination(thread, out)
    snap = {} if args.no_snapshot else snapshot.capture(thread.cwd)
    env_lines = snapshot.summary(snap)
    note = render.brief(thread, budget=args.budget)
    handoff = render.resume(
        thread,
        budget=args.budget + 1300,
        env_lines=env_lines,
        with_conflicts=args.conflicts,
        state_data=_existing_state(out),
    )
    written = bundle.write(thread, out, render.l1(thread), note, handoff, snapshot.render(snap) if snap else "")
    counts = thread.counts()
    state = render.assess(thread)
    print(f"已写出 {written}")
    print(f"  {counts.get('user', 0)} 轮 / {counts.get('tool_call', 0)} 次工具 / {len(thread.events)} 条事件")
    print(f"  简报 {len(note.encode('utf-8'))} 字节 · 接手稿 {len(handoff.encode('utf-8'))} 字节")
    print(f"  自动线索 {render.STATUS_LABELS.get(state['status'], state['status'])} —— {state['reason']}")
    source = Path(thread.source)
    if source.is_file():
        original = source.stat().st_size
        handoff_bytes = len(handoff.encode("utf-8"))
        print(f"  原文 {original} 字节 → 接手稿 {handoff_bytes} 字节（压缩 {original / max(handoff_bytes, 1):.0f} 倍）")
    if hits:
        summary = "、".join(f"{name}×{count}" for name, count in sorted(hits.items()))
        print(f"  已脱敏 {sum(hits.values())} 处：{summary}")
    if env_lines:
        print(f"  环境快照：{'；'.join(env_lines)}")
    return 0


def cmd_show(args) -> int:
    manifest = bundle.read(Path(args.bundle))
    events = manifest["_events"]
    if args.level == 0:
        thread = _thread_from(manifest, events)
        print(render.l0(thread))
        return 0
    if args.level == 1:
        print(manifest["_outline"])
        return 0
    if args.level == "b":
        if manifest["_brief"]:
            print(manifest["_brief"])
        else:
            print(render.brief(_thread_from(manifest, events)))
        return 0
    if args.level == "s":
        if manifest["_state"]:
            print(manifest["_state"])
        else:
            print("（包内没有 STATE.json；它应由交接方填写，见 README）")
        return 0
    if args.level == "r":
        if manifest["_resume"]:
            print(manifest["_resume"])
        else:
            print(render.resume(_thread_from(manifest, events)))
        return 0
    thread = _thread_from(manifest, events)
    print(render.l2(thread))
    return 0


def cmd_state(args) -> int:
    target = Path(args.bundle) / "STATE.json"
    if args.init:
        if target.exists() and not args.force:
            print(f"{target} 已存在，加 --force 覆盖")
            return 1
        manifest = bundle.read(Path(args.bundle))
        thread = _thread_from(manifest, manifest["_events"])
        data = state_file.skeleton(thread, render.assess(thread))
        state_file.write(target, data)
        _refresh_resume(Path(args.bundle))
        print(f"已生成骨架 {target}（{len(data)} 个字段，需要人来填判断）")
        return 0
    if args.check:
        problems = state_file.check(target)
        if problems:
            for problem in problems:
                print(f"[FAIL] {problem}")
            return 1
        _refresh_resume(Path(args.bundle))
        print(f"[ok  ] {target} 通过校验")
        return 0
    if not target.exists():
        raise SystemExit("包内没有 STATE.json，先运行 `baton state <包> --init`")
    print(target.read_text(encoding="utf-8-sig"))
    return 0


def cmd_read(args) -> int:
    manifest = bundle.read(Path(args.bundle))
    events = manifest["_events"]
    index = next((i for i, event in enumerate(events) if event.seq == args.seq), None)
    if index is None:
        raise SystemExit(f"没有 seq={args.seq} 的事件（共 {len(events)} 条）")
    low = max(0, index - args.context)
    high = min(len(events), index + args.context + 1)
    for event in events[low:high]:
        marker = ">>" if event.seq == args.seq else "  "
        print(f"{marker} [#{event.seq}] {event.ts} {event.kind} {event.name}".rstrip())
        if args.raw:
            if event.raw is None:
                print("    （该事件没有保留的宿主记录，可能来自旧格式包）")
            else:
                print(json.dumps(event.raw, ensure_ascii=False, indent=2))
            print()
            continue
        for value in (event.text, event.args, event.result):
            if value:
                print(f"    {value}")
        if event.kind == "meta" and event.raw is not None:
            print("    原始内容使用 --raw 查看。")
        print()
    return 0


def cmd_search(args) -> int:
    """Literal lookup across normalized fields and retained native records."""
    manifest = bundle.read(Path(args.bundle))
    needle = args.query.casefold()
    found = []
    for event in manifest["_events"]:
        values = (event.text, event.name, event.args, event.result,
                  json.dumps(event.raw, ensure_ascii=False) if event.raw is not None else "")
        matched = next((value for value in values if needle in value.casefold()), None)
        if matched is not None:
            found.append((event, matched))
    for event, value in found[:args.limit]:
        # Slice the original text only after matching, keeping output a bounded preview.
        index = value.casefold().find(needle)
        snippet = value[max(0, index - 60):index + len(args.query) + 160]
        print(f"[#{event.seq}] {event.kind} {event.ref}")
        print("  " + render.one_line(snippet, 240))
    print(f"命中 {len(found)} 条，显示 {min(len(found), args.limit)} 条；完整内容用 read <包> <seq> [--raw]。")
    return 0


def _nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("必须非负")
    return number


def _positive(value: str) -> int:
    number = _nonnegative(value)
    if number == 0:
        raise argparse.ArgumentTypeError("必须大于零")
    return number


def _query(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("关键词不能为空")
    return value


def cmd_trial(args) -> int:
    try:
        if args.out:
            path = trial.prepare(Path(args.out))
            print(f"已准备 4 类模拟接手材料，判分模板：{path}")
            print("仅将 receiver 下的单个场景交给未看过原文和判分标准的新会话；本命令未运行模型。")
        elif args.usage:
            print(json.dumps(trial_usage.inspect_rollout(Path(args.usage)), ensure_ascii=False, indent=2))
        elif args.compare:
            print(json.dumps(comparison.load(Path(args.compare)), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(trial.score(Path(args.score)), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from error
    return 0


def _thread_from(manifest: dict, events) -> "object":
    from .model import Thread

    return Thread(
        host=manifest.get("host", ""),
        thread_id=manifest.get("thread", ""),
        cwd=manifest.get("cwd", ""),
        source=manifest.get("source", ""),
        events=events,
        meta=manifest.get("metadata", {}),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="baton", description="把一个对话交接给另一个 agent")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="列出本机会话")
    listing.add_argument("--host", choices=SOURCES, default="codex")
    listing.add_argument("-n", "--limit", type=int, default=10)
    listing.add_argument("--json", action="store_true", help="输出用户会话 ID、实际活动时间和路径")
    listing.set_defaults(func=cmd_list)

    handoff = sub.add_parser("handoff", help="把本端用户会话送入另一端的固定收件箱")
    handoff.add_argument("--from", dest="source_host", choices=SOURCES, required=True)
    handoff.add_argument("--to", dest="target_host", choices=SOURCES, required=True)
    handoff.add_argument("--thread", default="latest", help="本端会话 ID、文件路径或 latest（排除子代理）")
    handoff.add_argument("--state", help="交接方填写的 STATE.json；省略时只生成待复核骨架")
    handoff.add_argument("--project", help="实际工程目录；未传时使用STATE.project或evidence.project")
    handoff.add_argument("--reply-to", help="返回原交接，保留完整链路")
    handoff.add_argument("--root", help="共享收件箱根目录，默认邻近工作区的 .memory/handoffs")
    handoff.add_argument("--note", default="", help="当前交接说明")
    handoff.add_argument("--json", action="store_true")
    handoff.set_defaults(func=cmd_handoff)

    inbox = sub.add_parser("inbox", help="查看发给本端的待接收交接")
    inbox.add_argument("--host", choices=SOURCES, required=True)
    inbox.add_argument("--all", action="store_true", help="包括已有回执的交接")
    inbox.add_argument("--root")
    inbox.add_argument("--json", action="store_true")
    inbox.set_defaults(func=cmd_inbox)

    receive = sub.add_parser("receive", help="核对交接、显示状态并写本端接收回执")
    receive.add_argument("transfer", nargs="?", default="latest")
    receive.add_argument("--host", choices=SOURCES, required=True)
    receive.add_argument("--root")
    receive.add_argument("--note", default="")
    receive.add_argument("--json", action="store_true")
    receive.set_defaults(func=cmd_receive)

    take = sub.add_parser("take", help="接手或恢复当前任务，保留可编辑状态草稿")
    take.add_argument("transfer", nargs="?", default="latest")
    take.add_argument("--host", choices=SOURCES, required=True)
    take.add_argument("--root")
    take.add_argument("--switch", action="store_true", help="显式切换到另一交接，保留原草稿")
    take.add_argument("--thread", help="绑定本端真实会话；绑定后reply无需再填ID")
    take.add_argument("--project", help="当前机器的工程目录，覆盖发送时目录以核对文件")
    take.add_argument("--json", action="store_true")
    take.set_defaults(func=cmd_flow)

    task = sub.add_parser("task", help="查看本端当前任务、草稿路径和返回状态")
    task.add_argument("--host", choices=SOURCES, required=True)
    task.add_argument("--root")
    task.add_argument("--json", action="store_true")
    task.set_defaults(func=cmd_flow)

    reply = sub.add_parser("reply", help="把更新后的当前任务返回原交接方，自动关联链路")
    reply.add_argument("--host", choices=SOURCES, required=True)
    reply.add_argument("--thread", help="本端实际会话ID；省略时使用take绑定的会话，多会话时不要使用latest")
    reply.add_argument("--state", help="使用另一个已更新的状态文件；默认取当前任务的草稿")
    reply.add_argument("--note", default="")
    reply.add_argument("--root")
    reply.add_argument("--json", action="store_true")
    reply.set_defaults(func=cmd_flow)

    export = sub.add_parser("export", help="导出一个会话为交接包")
    export.add_argument("thread", help="thread id、文件名片段，或 rollout 文件路径")
    export.add_argument("--host", choices=SOURCES, default="codex")
    export.add_argument("--out", help="输出目录")
    export.add_argument("--budget", type=_positive, default=5200, help="简报字节预算")
    export.add_argument("--no-snapshot", action="store_true", help="不抓环境快照")
    export.add_argument("--conflicts", action="store_true", help="附带结论冲突检测（实验性，误报较多）")
    export.set_defaults(func=cmd_export)

    show = sub.add_parser("show", help="按密度读取交接包")
    show.add_argument("bundle")
    show.add_argument("--level", type=lambda v: v if v in ("b", "r", "s") else int(v), choices=(0, 1, 2, "b", "r", "s"), default=1)
    show.set_defaults(func=cmd_show)

    check = sub.add_parser("doctor", help="自检：解析器、本机会话、格式版本")
    check.set_defaults(func=lambda _args: doctor.run())

    read = sub.add_parser("read", help="按 seq 展开一条事件及其上下文")
    read.add_argument("bundle")
    read.add_argument("seq", type=int)
    read.add_argument("--context", type=_nonnegative, default=2)
    read.add_argument("--raw", action="store_true", help="显示保留的宿主记录（脱敏后），不截断")
    read.set_defaults(func=cmd_read)

    search = sub.add_parser("search", help="按关键词定位事件（字面匹配，不区分大小写）")
    search.add_argument("bundle")
    search.add_argument("query", type=_query)
    search.add_argument("--limit", type=_positive, default=20)
    search.set_defaults(func=cmd_search)

    state_cmd = sub.add_parser("state", help="状态文件：生成骨架 / 校验 / 查看")
    state_cmd.add_argument("bundle")
    state_cmd.add_argument("--init", action="store_true", help="从事件流生成骨架")
    state_cmd.add_argument("--check", action="store_true", help="校验必填字段")
    state_cmd.add_argument("--force", action="store_true", help="覆盖已存在的 STATE.json")
    state_cmd.set_defaults(func=cmd_state)

    trial_cmd = sub.add_parser("trial", help="准备模拟接手盲测材料，或汇总人工判分（不调用模型）")
    trial_action = trial_cmd.add_mutually_exclusive_group(required=True)
    trial_action.add_argument("--out", help="新建测试目录，含隔离的 receiver 和 judge 材料")
    trial_action.add_argument("--score", help="汇总已填写的 judge/judgements.json")
    trial_action.add_argument("--usage", help="读取本地 Codex rollout 的接手会话实测 token（不输出会话正文）")
    trial_action.add_argument("--compare", help="汇总完整三条件配对案例的人工判分及实测 token 比例")
    trial_cmd.set_defaults(func=cmd_trial)

    eval_cmd = sub.add_parser("eval", help="统计规则线索覆盖率（不代表判断正确率或接手成功率）")
    eval_cmd.add_argument("--host", choices=SOURCES, default="codex")
    eval_cmd.set_defaults(func=lambda args: measure.run(args.host))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
