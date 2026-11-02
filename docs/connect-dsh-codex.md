# DSH ↔ Codex 双向接手

优先目标是让两端能实际接收并继续同一任务。共享记忆保留长期结论；交接收件箱保存本次完整事件、当前状态与接收回执。

## 本机入口

无需 pip 安装或修改 PATH。使用项目内的 PowerShell 入口，它优先使用 DSH 自带 Python：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\baton.ps1" --help
```

后续例子中的 `baton` 可以替换为上面这条入口命令。已安装包时也可直接使用 baton。

共享收件箱默认是相邻工作区的 `.memory/handoffs`；若未找到共享记忆目录则使用 `~/.baton/handoffs`。可用 `--root` 或 BATON_HOME 指定其他本地共享目录。

## 日常接手：take / task / reply

第一份交接仍由 `handoff` 发出。接收方日常只需：

```powershell
baton take --host dsh --thread <本次真实DSH会话ID>
baton task --host dsh
# 助手核对工作区并继续操作，更新输出中标出的可编辑 STATE.json。
baton reply --host dsh
```

另一端执行 `baton take --host codex` 即可接回。对用户可以表达为“接手最新 baton 任务”或“更新进度后交回 Codex”；接到指令的助手按这三条入口操作。当前仍需在目标宿主触发接手，没有后台自动唤醒。

`take` 核对并接收材料，在 `work/<host>/<交接ID>/STATE.json` 创建可编辑草稿，封存原包保持不变。再次运行默认恢复尚未完成、尚未返回的当前任务，保留已经改过的草稿；新来信不会挤掉正在做的任务。没有未完成的当前任务、同时有多封待收包时，`take latest` 只接手其中最新一封，其余保持待收；接手其他包请显式给 ID。多个任务之间可显式 `take <ID> --host dsh --switch`，原草稿保留。

`task` 只读显示当前任务、工作目录、草稿路径、工程新鲜度及是否已返回；草稿格式损坏时显示修复位置。接手方仍需核对当前文件、测试和用户要求，工具不判断草稿中的事实真假。

`reply` 默认读取当前草稿，自动填写目标端和 reply-to。首次 `take --thread <ID>` 可绑定本端真实会话，后续 `reply` 无需再次填写；未绑定时仍可 `reply --thread <ID>`。绑定冲突会报错，避免同时开着多个聊天时导出错会话。重复运行会返回既有包 ID，不重复发送，也不会偷偷将后改的草稿再发一次；如确需追加一个返回，请显式使用 `handoff`。

首次接手在回执写入后意外中断时，可用 `take <ID> --host dsh` 恢复草稿。存在未完成写入锁时命令会停下，先检查 `active/<host>.lock`；不会自行删除锁或重置状态。

## 工程现场与当前焦点

发送时指定实际工程目录：

```powershell
baton handoff --from codex --to dsh --thread <当前会话ID> --state <STATE路径> --project <实际工程目录>
```

也可把目录写入 STATE.project；兼容已有 evidence.project。新包增加可校验的 PROJECT.json 和 FOCUS.md，旧包仍可读。PROJECT 保存提交、分支、未提交条目、相关文件及改动文件的哈希；最多64个文件、单文件最多2MiB，省略、无法读取、敏感文件或链接都会明确标记。不保存文件正文，也不复制仓库或运行旧命令。

接手时自动比较已记录范围，报告“范围一致 / 已变化 / 部分无法核对 / 目录不存在”。即使 Git 状态仍为同一个 M，文件内容变化也会被哈希检测。目录不同可用 take --project 显式映射。匹配只覆盖记录的范围，不表示整个工程或测试结论全部有效。

STATE 可选增加这些字段，由执行助手按当前任务填写：

```json
{
  "project": "实际工程目录",
  "context": {"files": ["src/app.py"], "refs": [42, 47]},
  "checks": [{"command": "python -m unittest", "exit_code": 1, "observed_at": "实际运行时间", "log": "tests-output.txt"}]
}
```

refs 是本次来源会话的原文编号，需先核对；也可使用 context.since 指定任务起点。FOCUS 优先展示当前任务、限制、下一步、相关文件和所选证据，并将工具调用与对应结果一起展示。未选证据时展示近期事件，完整原文始终保留。恢复草稿后会生成当前焦点，而非重复依赖旧接手稿。草稿选取绑定本端会话的新事件时，标记其为实时且尚未封存的编号，并对实时片段做脱敏；不能套用到接收的旧包。跨宿主返回时旧编号带来源与包ID留存，不会错误套用到新会话。

checks 是交接方填报；日志哈希仅能确认日志有无变化，不能证明旧测试覆盖当前代码。新版本不额外调用模型生成语义摘要，也不靠关键词判断任务目标。

## 更少的手动步骤

handoff/reply 输出一段可直接转发的接手指令，包含本机入口和准确包ID；目标助手填写自己的真实会话ID并绑定，用户不必查ID、修改JSON或复制状态路径。助手的完整操作约定见 [agent-handoff.md](agent-handoff.md)。仍需在另一端发一次指令，尚无后台自动唤醒或跨机器文件同步。

## 项目的实际价值

- 换助手时携带目标、进度、限制和待做事项，减少重新解释和重复排查的机会。
- 让两端可以分工并交回结果，同一项目继续使用已有文件和工作区。
- 保留完整原始记录与可回查引用，接手方能够核对摘要遗漏或未经验证的结论。
- 用接收回执、完整性校验及返回链定位交接到了哪一步，便于恢复中断。
- 共享记忆保存长期结论，可编辑状态保存当前进度，封存包保留交接当时的证据。

已验证本机真实 Codex→DSH→Codex 往返及两端回执。减少解释时间、降低重复劳动是产品目标，尚无普遍量化结论；既有 token 试验不能视为保证节省比例。文件和附件仍依赖原工作区，多机器同步另需设计。

## Codex 交给 DSH

交接方整理当前 STATE.json（目标、已验证事实、待核验声明、下一步和限制），再执行：

```powershell
baton list --host codex
baton handoff --from codex --to dsh --thread <当前Codex会话ID> --state <STATE.json路径>
```

`--thread latest` 选择本端最近有活动的真实用户会话，排除子代理和审查线程；同时开着多个任务时，应显式给 ID。排序读取记录内的时间，不仅依赖 Windows 上可能滞后的文件修改时间。

然后在 DSH 中说：

> 接收发给 DSH 的最新 baton 交接，先核对状态与原文，再继续当前任务。

DSH 执行：

```powershell
baton inbox --host dsh
baton receive latest --host dsh
```

receive 会核对事件、状态和接手稿完整性，显示当前状态并写回执。它只读取任务数据，不运行历史命令。已有回执的交接可按完整 ID 再读；latest 只取尚未接收的条目。

## DSH 返回 Codex

DSH 更新自己的状态后执行：

```powershell
baton handoff --from dsh --to codex --thread <当前DSH会话ID> --reply-to <刚接收的交接ID> --state <更新后的STATE.json>
```

Codex 执行：

```powershell
baton inbox --host codex
baton receive latest --host codex
```

返回包会导出真实 DSH 原生会话，沿用 chain 并指向原交接；两端各有接收回执。可以多次往返，既有包保持不变。发送到同端、未接收就返回、方向不符或材料改变会明确报错。

## 可核对的文件

每次交接位于 `items/<交接ID>/`：

- TRANSFER.json：来源、目标、原生会话、工作目录、时间、链路和文件哈希。
- packet/：完整事件、接手稿和 STATE 等材料。
- RECEIPT.json：目标端确认接收的时间、host 和对应清单校验值。

校验用于发现材料意外变化，回执是本机工作流记录，并非远程身份认证。目录中的文件和附件仍依赖原工作区；这套流程不复制整个仓库，也不会把原生聊天日志改造成另一端的私有格式。

## 本机 DSH 命令行

本机安装包的原生命令支持 headless 单任务与 --session-id 续接。headless 与桌面端配置可能分开；本次 headless 启动明确返回 MISSING_CREDENTIAL，因此不能假定桌面模型配置会自动复用。可在已经可用的 DSH 桌面会话执行收发命令，无需为 baton 添加凭据。

baton 本身不启动宿主、不读取密钥、不修改既有聊天日志。启动另一宿主并发送任务是单独的操作，收件箱不是后台自动唤醒服务。

## Windows 目录访问

交接目录应继承共享工作区的访问权限。Windows Python 3.12.10+ 的临时目录默认 0700 可能产生仅创建方可读的目录；当前发送实现改用普通 mkdir 后原子发布。测试夹具也统一使用 `tests/support.py` 中的工作区临时目录实现，在 Windows 上继承父目录权限，并保留独立目录及限定范围清理；不会修改已有目录的权限。`trial --out` 本身使用普通 mkdir，但其父目录也必须可写，不能放进已受限的私有临时目录。若早期测试包无法读取，应保留诊断并重新生成交接，不要求放宽整个工作区的权限。
