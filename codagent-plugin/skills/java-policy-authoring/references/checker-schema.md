# Checker schema

只把以下结构写入候选规则的 `metadata.checks` 数组。所有 checker 都可使用：

- `include_paths` / `exclude_paths`：glob 数组，路径统一写 `/`；
- `when_pattern`：只有内容命中该正则时才应用；
- `when_terms`：任一词出现时应用；
- `flags`：`ignorecase`、`multiline`、`dotall`、`verbose`；
- `severity`、`blocking`、`message`：覆盖项必须在审阅文件中可见。

## regex_forbid

发现任一 `pattern` 就失败，适合禁止稳定的局部语法。

```json
{
  "type": "regex_forbid",
  "patterns": ["\\bnew\\s+Thread\\s*\\("],
  "include_paths": ["**/*.java"],
  "message": "禁止直接创建线程"
}
```

## regex_require

应用条件成立后必须找到要求模式。默认 `require_all: true`；它检查整个文件，不理解 Java 作用域，因此不要用它假装证明“每个 catch 都有日志”等局部控制流性质。

```json
{
  "type": "regex_require",
  "when_pattern": "@RestController",
  "patterns": ["@Validated"],
  "require_all": true,
  "include_paths": ["**/*Controller.java"]
}
```

## path_allow / path_forbid

分别要求目标路径命中允许列表，或不得命中禁止列表。

```json
{
  "type": "path_allow",
  "allowed_paths": ["order-api/src/main/java/**"],
  "when_terms": ["OrderController"]
}
```

```json
{
  "type": "path_forbid",
  "forbidden_paths": ["**/controller/**/*Repository.java"]
}
```

## companion_change

当本次变更集合命中 `trigger_paths` 时，要求同一变更集合命中 `required_paths`。默认全部要求都要命中。

```json
{
  "type": "companion_change",
  "trigger_paths": ["**/*Controller.java"],
  "required_paths": ["**/*ControllerTest.java"],
  "require_all": true
}
```

## ai_review

用于无法确定性验证的规则，可提供更聚焦的审查提示。它只产生待 AI 审查证据，不是程序化通过。

```json
{
  "type": "ai_review",
  "include_paths": ["**/*.java"],
  "prompt": "逐个 catch 块确认异常已按项目日志规范处理，并给出行号证据。"
}
```

草案必须同时准备至少一个应通过和一个应失败的最小样例。若 checker 对样例无法稳定区分，删除该 checker 草案并改用 AI review。

激活器会拒绝超长正则和常见嵌套重复量词，降低 command Hook 被灾难性回溯拖到超时的风险；这不是完整的 ReDoS 证明。正则越复杂，越应改用 AI review 或公司批准的 Java AST/静态分析检查器。
