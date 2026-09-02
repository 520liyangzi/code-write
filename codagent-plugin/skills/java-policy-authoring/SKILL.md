---
name: java-policy-authoring
description: 导入、更新或维护 Java 公司/部门/项目编码规范时，把 Markdown 提取为待审阅规则，并为能可靠自动判断的条款生成确定性 checker 草案。用户谈论规范入库、规则更新、REVIEW_ME、checker 草案或 Policy Kit 维护时使用；普通业务编码不要使用。
---

# Java Policy Authoring

这是规则维护流程，不是业务编码流程。所有自动提取内容都只是候选；绝不替用户勾选接受，绝不把未批准规则写入全局 MD 或正式规则库。

## 流程

1. 确认输入目录只含本次真实 Markdown 规范，按 company、department、project 分目录；测试示例不得混入。
2. 运行 `policy.ps1 prepare`。读取原文、`candidates.json` 和生成的 `REVIEW_ME.md`，保留来源、章节、行号和原句。
3. 逐条判断能否由当前检查器可靠验证。读取 [checker schema](references/checker-schema.md) 后，只为低误报、低漏报且无需业务语义猜测的条款，在候选规则的 `metadata.checks` 中生成草案。
4. 复杂控制流、空值来源、跨方法数据流、业务语义和无法精确表达的规则不生成伪正则；保留为 AI review，并在审阅备注中说明原因。
5. 运行 `policy.ps1 review`，让每条可执行草案以 JSON 显示在 `REVIEW_ME.md`。运行测试或用最小正反样例验证每个草案；无测试证据的 checker 不得标为确定性。
6. 向用户交付 `REVIEW_ME.md`，列出候选数量、带 checker 草案数量、仅 AI review 数量、疑似重复/冲突和测试结果。等待用户亲自勾选。
7. 只有用户完成勾选后才能运行 `activate`；激活后再安装或 `install.ps1 -Update`。

不得直接改 `approved-rules.json`。修改草案时编辑 `candidates.json` 中对应规则的 `metadata.checks`，随后重新运行 `review`。若自然语言正文也需修改，让用户使用 `REVIEW_ME.md` 的“修改后接受”。

## 安全准则

- Checker 的适用路径要尽量收窄；没有明确范围时不要猜项目模块。
- 正则只用于局部、稳定的语法特征。Java 嵌套语法或跨语句关系优先 AI review，除非已有解析器型检查器。
- `blocking` 默认只由规则严重度和运行时配置决定；需要覆盖时必须在草案中显式展示并说明。
- `companion_change` 只检查本次变更集合，不证明磁盘上文件是否已经存在；规则原意若是“文件必须存在”，不要错误编译成配套变更。
- 所有规则继续携带来源引用；不得把通用 Java 建议冒充公司原文。
