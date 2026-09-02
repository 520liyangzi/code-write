---
example_only: true
status: test_only
scope: company
---

# 模拟 Java 安全编码规范（仅测试）

> 本文档为虚构测试数据，不代表任何公司要求，禁止用于正式激活。

## TEST-SEC-001 SQL 参数化

来自请求、消息或外部系统的数据不得通过字符串拼接进入 SQL，必须使用参数绑定。

## TEST-SEC-002 敏感日志

认证凭据、访问令牌和完整身份证件号码不得写入应用日志。
