# 仅用于测试的模拟规范

本目录中的文档是虚构示例，仅用于验证 `prepare → REVIEW_ME → activate` 流程。

它们不是公司规范，默认不会被导入或激活。不要把这里的规则复制到 Codagent 全局 MD，也不要与真实 `policy-sources/` 混放。只有在隔离的测试目录中显式执行 `prepare --source examples/test-policies` 时才使用它们；测试后不要安装生成结果。

`Java结构化编码规范-仅测试.md` 专门覆盖 `规则编号 + 【级别】/【描述】/【反例】/【正例】` 格式，并用于验证变量命名、顶层 public 类型 Javadoc、反序列化和格式化字符串四类场景的召回。
