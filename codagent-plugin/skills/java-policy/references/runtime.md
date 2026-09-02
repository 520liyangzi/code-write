# Java Policy Kit 运行时命令

自动安装器或手工部署包导出器会把下面命令块中的占位内容替换为当前机器上的绝对命令。若命令块仍不是可执行的 PowerShell 命令，说明这是源码模板而不是可部署版本；不要直接执行，改用 `PreToolUse` Hook 的首次阻断取证流程。

在一个逻辑改动单元的第一次 `Edit`、`Write` 或 `MultiEdit` 之前，若宿主提供了真实 `session_id`，执行：

```text
__POLICYKIT_SEARCH_COMMAND__ --query "<本次改动意图和涉及的 API>" --file "<目标文件>" --session "<真实 session_id>" --receipt --json
```

- `--file` 必须是即将写入的文件；相对路径以当前项目目录为准。
- 不得猜测或复用其他会话的 ID。
- 只有返回 `receipt.receipt_issued: true` 才表示主动取证成功。
- 返回 `receipt.status: no_applicable_rule`（顶层 `status` 会与它保持一致）仍然是一次成功检索，应报告“已检索，无专门规范”。
- 返回 `receipt.blocking: true`、命令失败或 JSON 无法解析时，停止写入并如实报告。
- receipt 是目标文件绑定、短时有效且一次性的；每次真实写入后都需要为下一次写入重新取证。

若当前宿主没有办法把真实 `session_id` 传给 Skill，直接发起第一次写入即可。`PreToolUse` 会先阻断并把检索结果放入上下文；报告结果后，依据该结果重试同一目标文件。
