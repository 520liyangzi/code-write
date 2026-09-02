<!-- CODAGENT-JAVA-POLICY:START -->

## Java 规范执行入口

创建或修改 Java 代码时，必须使用 `java-policy` Skill：在每个逻辑改动单元写入前检索已批准规范，写入后报告实际检查结果。结束 Java 任务前必须使用 `java-review` Skill 审查完整变更。

不得使用 Shell 重定向、Python/Node 写文件脚本或会修改源码的外部命令绕过 Edit/Write Hook。

不得把“没有检索”“检索失败”“只经过 AI 判断”表述为已经严格通过公司规范。CodeGraph 仅在可用且有帮助时使用，不是强制依赖。

> 激活真实规范后，请把 `.policy-work/GLOBAL_MD_BLOCK.md` 中生成的完整区块替换到这里；不要把全部原始规范粘贴进全局 MD。

<!-- CODAGENT-JAVA-POLICY:END -->
