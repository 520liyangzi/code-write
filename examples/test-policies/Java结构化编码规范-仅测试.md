---
example_only: true
status: test_only
scope: company
---

# 模拟 Java 结构化编码规范（仅测试）

> 本文档仅供检索与结构化抽取测试，不代表任何公司要求。

### 3.1.1 G.NAM.01 变量采用小驼峰命名

**【级别】** 要求

**【描述】**

局部变量、方法参数和非静态字段必须采用小驼峰（camelCase）命名，名称应表达实际含义。

**【反例】**

```java
String User_Name = request.getParameter("name");
```

**【正例】**

```java
String userName = request.getParameter("name");
```

### 3.2.1 G.CMT.02 顶层 public 类的 Javadoc 应该包含功能说明和创建日期/版本信息

**【级别】** 要求

**【描述】**

每次创建顶层 public class、interface、enum 或 record 时，都应提供类级 Javadoc，说明主要功能并记录创建日期或版本信息。

**【反例】**

```java
public class OrderService {
}
```

**【正例】**

```java
/**
 * 订单领域服务。
 *
 * @since 2026-09-02
 */
public class OrderService {
}
```

### 3.8.1 G.SER.01 禁止直接将外部数据进行反序列化

**【级别】** 禁止

**【描述】**

来自 HTTP 请求、消息、文件或其他不可信来源的数据不得直接传入 Java 原生反序列化或对象映射入口；必须先完成来源校验、类型白名单和大小限制。

**【反例】**

```java
Object value = new ObjectInputStream(request.getInputStream()).readObject();
```

**【正例】**

```java
ValidatedOrder order = validatedOrderDecoder.decode(request.getInputStream());
```

### 3.12.3 G.EDV.02 禁止直接使用外部数据构造格式化字符串

**【级别】** 要求

**【描述】**

Java 中的 Format 可以将对象按指定格式转为字符串。当攻击者能够控制格式化字符串时，可能导致信息泄露、拒绝服务或功能异常。因此格式模板必须由程序定义，外部数据只能作为待格式化的参数。

**【反例】**

```java
String format = request.getParameter("format");
String formattedValue = String.format(format, getData());
```

直接使用请求参数作为格式化字符串，攻击者可传入与对象类型不匹配的格式。

**【正例】**

```java
String formattedValue = String.format("my format: %s", getData());
```

格式化字符串由程序固定定义，外部数据只作为 `%s` 对应的值。
