# Java Policy Kit 手工复制清单

本目录是已激活规范的自包含部署包。导出过程没有读取、修改或创建任何 Codagent 目录。

> 重要：Hook 和 Skill 中已经写入本包运行时的绝对路径。完成部署后不要移动或重命名本目录；如需换位置，请在目标位置重新运行导出命令。

## 复制前

- [ ] 确认当前包路径为：`__OUTPUT_ROOT__`
- [ ] 确认导出时绑定的 Python 命令为：`__PYTHON_COMMAND__`
- [ ] 备份 Codagent 的全局 MD、现有 Skills 和 Hook 配置。
- [ ] 确认 `runtime\.policy-work\approved-rules.json` 中只有已经人工批准的规则。
- [ ] 在 PowerShell 中运行下面的自检命令，结果应全部通过：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "__POLICY_SCRIPT__" -PythonCommand "__PYTHON_COMMAND__" -PolicyHome "__RUNTIME_ROOT__" doctor
```

## 手工复制

- [ ] 将 `skills\java-policy`、`skills\java-review`、`skills\java-policy-authoring` 三个完整目录复制到 Codagent 的全局 Skills 目录。若同名目录已存在，先备份并人工比较，不要直接覆盖。
- [ ] 打开 `CLAUDE_MD_BLOCK.md`，只把标记范围内的内容合并到 Codagent 全局 MD；不要覆盖全局 MD 的其他内容。
- [ ] 按公司 Codagent 的 Hook 配置方式合并 `hooks\hooks.json`。保留既有 Hook，不要用本文件整体覆盖其他团队配置。
- [ ] 合并后、重载前，验证“Codagent 实际读取的最终 Hook 配置”，而不只是本包的 `hooks.json`。若实际文件是严格 JSON，先运行下面的无 BOM、严格 UTF-8 与 JSON 解析检查；若公司 Codagent 提供配置校验命令，再运行它。任一检查失败时立即恢复备份，不要重载。

```powershell
$ActualHookPath = "D:\path\to\actual-codagent-hooks.json"
$HookBytes = [System.IO.File]::ReadAllBytes($ActualHookPath)
$HasUtf8Bom = $HookBytes.Length -ge 3 -and $HookBytes[0] -eq 0xEF -and $HookBytes[1] -eq 0xBB -and $HookBytes[2] -eq 0xBF
if ($HasUtf8Bom) { throw "Hook JSON must be UTF-8 without BOM: $ActualHookPath" }
$StrictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
$HookText = $StrictUtf8.GetString($HookBytes)
if ([string]::IsNullOrWhiteSpace($HookText)) { throw "Hook JSON is empty: $ActualHookPath" }
$ParsedHook = $HookText | ConvertFrom-Json
if ($null -eq $ParsedHook) { throw "Hook JSON root cannot be null: $ActualHookPath" }
```

- [ ] 保留整个 `runtime` 目录及本包位置不变。Hooks、主动检索命令和审计产物都依赖这里的绝对路径。

## 复制后验证

- [ ] 只有最终 Hook 配置解析，以及（若提供）公司原生校验都通过后，才重新启动 Codagent，使 Skills 和 Hooks 重新加载。
- [ ] 用一个真实 Java 文件执行一次规范检索：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "__POLICY_SCRIPT__" -PythonCommand "__PYTHON_COMMAND__" -PolicyHome "__RUNTIME_ROOT__" search --query "捕获异常并记录日志" --file "D:\path\to\Example.java" --json
```

- [ ] 让 Codagent 尝试一次小型 Java 修改，确认首次写入能收到“规范凭据”，写入后能收到检查结果。
- [ ] 结束一次会话，确认 `runtime\.policy-work\audit\reports` 下生成报告。
- [ ] 确认报告明确区分“命中规范”“无专门规范”“硬检查结果”和“AI 语义审查”。

查看最近的审计报告时也必须使用导出时绑定的同一个 Python 命令：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "__POLICY_SCRIPT__" -PythonCommand "__PYTHON_COMMAND__" -PolicyHome "__RUNTIME_ROOT__" report
```

## 回退

- [ ] 从备份恢复原 Skills、Hook 配置和全局 MD。
- [ ] 回退时可以停止引用本包，但不要在仍有 Hook 指向本包时移动或删除 `runtime`。
