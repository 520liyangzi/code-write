---
example_only: true
status: test_only
scope: company
---

# 模拟 Java 编码规范（仅测试）

> 本文档为虚构测试数据，不代表任何公司要求，禁止用于正式激活。

## TEST-CODE-001 异常处理

捕获异常后不得使用 `printStackTrace()`；应根据调用边界选择记录日志、转换异常或重新抛出，避免同一异常在多层重复记录。

## TEST-CODE-002 不可变 Map

传给 `Map.of` 和 `Map.ofEntries` 的键和值必须确定非空；无法证明非空时，应先校验、过滤或使用业务允许的默认值。
