# Java Policy Kit 保姆级使用教程

这套工具用于把 Java 编码规范 Markdown 转换成可审核、可检索、可接入 Codagent 的规则库。

你不需要手工拆词，也不需要把整份规范塞进 Codagent。正常流程是：上传 Markdown，检查系统抽取出的完整规则，批准后生成正式索引；Codagent 写代码时再按任务、文件和代码片段读取相关规则。

> 如果你第一次使用，按本文从上到下操作即可。更完整的设计、安全边界、Hook 行为和维护细节见 [instruction.md](instruction.md)。

## 一句话流程

```text
准备环境
  → 拉取代码
  → 初始化目录
  → 可选配置 OpenAI / 本地数据库
  → 启动页面
  → 上传规范 Markdown
  → 生成候选规则
  → 审批规则
  → 激活规则库
  → 用检索沙盒验收
  → 导出并接入 Codagent
```

## 1. 最后会得到什么

完成一次完整操作后，项目中会生成：

| 文件 | 作用 | 是否直接手改 |
|---|---|---|
| `policy-sources/` | 你导入的公司、部门、项目 Markdown | 可以，以原始规范为准 |
| `.policy-work/candidates.json` | 自动抽取出的候选规则 | 通常不要手改 |
| `.policy-work/REVIEW_ME.md` | 人工审批记录 | 可以通过页面或文本编辑器修改 |
| `.policy-work/approved-rules.json` | 已批准的正式规则 | 不要手改 |
| `.policy-work/search-index.db` | BM25 与可选向量检索索引 | 不要手改 |
| `.policy-work/GLOBAL_MD_BLOCK.md` | 需要合并到 Codagent 全局 MD 的流程约束 | 激活后检查并复制 |

`policy-sources/` 和 `.policy-work/` 已被 Git 忽略，不会因为普通 `git add .` 把公司规范、审批数据、向量或本地数据库提交到公开仓库。

## 2. 先选一种使用方式

### 方式 A：先在页面里试用

只需要 Git、Python 3.10+ 和浏览器。适合先验证 Markdown 抽取、审批和检索效果。

### 方式 B：完整接入 Codagent

先完成页面试用，再准备 JDK 21、Maven，并确认公司 Codagent 实际使用的三个位置：

1. 全局 Skills 目录；
2. 全局 MD 文件；
3. Hooks 配置文件或配置入口。

公司魔改版 Codagent 的目录不一定是 `%USERPROFILE%\.codagent`，不要直接照搬 Claude Code 的默认路径。

## 3. 第一步：检查电脑环境

以下教程优先按 Windows PowerShell 编写。

打开 PowerShell，逐条执行：

```powershell
git --version
python --version
```

最低要求：

- Git 能正常执行；
- Python 版本不低于 3.10；
- 浏览器能访问 `127.0.0.1`。

完整接入 Codagent 时再检查：

```powershell
java -version
mvn -version
```

推荐使用 JDK 21，并确保 Maven 能从当前 PowerShell 直接运行。

如果 `python` 命令不存在，但电脑已经安装 Python，可以先找到 `python.exe` 的绝对路径，后续这样指定：

```powershell
.\scripts\policy.ps1 -PythonCommand "C:\Python312\python.exe" ui
```

## 4. 第二步：拉取项目

首次使用：

```powershell
cd D:\workspace
git clone https://github.com/520liyangzi/code-write.git
cd code-write
```

如果已经拉取过：

```powershell
cd D:\workspace\code-write
git pull origin main
```

确认当前位置正确：

```powershell
Get-Location
Get-ChildItem
```

应当能看到 `policykit.json`、`scripts`、`src`、`instruction.md` 等文件。

## 5. 第三步：初始化工作目录

如果 PowerShell 阻止脚本运行，只对当前窗口临时放开：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

然后初始化：

```powershell
.\scripts\policy.ps1 init
```

正常输出类似：

```text
已初始化：D:\workspace\code-write
配置文件：D:\workspace\code-write\policykit.json
请把规范 Markdown 放入：D:\workspace\code-write\policy-sources
```

此时会生成：

```text
policy-sources/
├── company/
├── department/
└── project/

.policy-work/
```

三个范围的含义：

| 范围 | 放什么 |
|---|---|
| `company` | 全公司通用 Java、安全、性能规范 |
| `department` | 部门或研发组内部约定 |
| `project` | 单个项目的目录、框架和业务约定 |

系统不会自动认为“项目规则覆盖公司规则”。如果不同范围存在冲突，必须在审批时补清楚适用条件，或者拒绝已经过时的规则。

## 6. 第四步：准备 Markdown

你的文档可以直接使用下面这种结构：

````markdown
### 3.12.3 G.EDV.02 禁止直接使用外部数据构造格式化字符串

**【级别】** 要求

**【描述】**

格式模板必须由程序定义，外部数据只能作为待格式化参数。

**【反例】**

```java
String format = request.getParameter("format");
String value = String.format(format, getData());
```

**【正例】**

```java
String value = String.format("my format: %s", getData());
```
````

建议遵守以下规则：

1. 文件编码使用 UTF-8；
2. 一条规则使用一个三级标题；
3. 标题中保留稳定规则编号，例如 `G.EDV.02`；
4. 尽量提供级别、描述、反例和正例；
5. 触发条件、例外和适用范围要写完整；
6. 不要让新旧两个版本的同一份规范同时存在；
7. 不要把目录页、图片占位符或纯背景说明误当成规则正文。

仓库提供了一份可以直接观察格式的测试文档：

[examples/test-policies/Java结构化编码规范-仅测试.md](examples/test-policies/Java结构化编码规范-仅测试.md)

它覆盖：

- `G.NAM.01`：变量采用小驼峰命名；
- `G.CMT.02`：顶层 `public` 类型需要类级 Javadoc；
- `G.SER.01`：禁止直接反序列化外部数据；
- `G.EDV.02`：禁止用外部数据构造格式化字符串。

> 该文件仅用于测试，不代表真实公司要求。正式环境不要把 `examples/test-policies/` 整个目录导入规则库。

## 7. 第五步：是否现在启用 OpenAI 和数据库

第一次只想看页面，可以保持默认配置，直接跳到下一节。默认模式完全使用本地结构化解析和 BM25，不需要联网。

如果确定要使用大模型增强或向量，建议在第一次“生成候选规则”之前配置。否则后面再启用时，规则检索元数据发生变化，部分规则可能需要重新确认。

推荐顺序：

1. 先决定是否启用 OpenAI；
2. 再决定是否同步本地数据库；
3. 配置完成后启动页面；
4. 最后上传和生成候选规则。

详细配置见本文第 16、17 节。

## 8. 第六步：启动 Policy Studio 页面

在项目根目录执行：

```powershell
.\scripts\policy.ps1 ui
```

正常情况下浏览器会自动打开：

```text
http://127.0.0.1:8765/
```

注意：

- 启动页面的 PowerShell 窗口要保持运行；
- 使用完成后回到该窗口按 `Ctrl+C` 停止；
- 页面只允许本机访问，不要把监听地址改成 `0.0.0.0`；
- 页面不加载外部 CDN。

页面没有自动打开时，手工复制上面的地址到浏览器。端口被占用时：

```powershell
.\scripts\policy.ps1 ui --port 8899 --no-open
```

然后访问：

```text
http://127.0.0.1:8899/
```

## 9. 第七步：上传规范并生成候选规则

进入页面后，严格按下面操作。

### 9.1 上传 Markdown

1. 找到“导入规范并审阅候选”；
2. 在“文档范围”中选择公司级、部门级或项目级；
3. 点击“选择 .md 文件”，也可以把文件拖进虚线框；
4. 检查文件列表；
5. 点击“导入选中文档”；
6. 等待右下角出现“文档已导入”。

一次选择的文件会使用同一个范围。公司规范和项目规范应分两次上传。

页面为防止误覆盖，会拒绝同名文件。如果是在更新规范，请先在文件资源管理器中备份并明确替换 `policy-sources\company`、`department` 或 `project` 中的旧文件，再回页面生成候选。

### 9.2 生成候选

点击右上方“生成候选规则”。

正常提示应包含：

- 扫描到的 Markdown 数量；
- 候选规则数量；
- 保留了多少条已有决定；
- 新增或变化的待处理规则数量；
- 大模型增强使用了多少缓存、生成了多少新结果（仅启用 AI 时）。

生成完成后，页面会出现规则卡片。结构化规则卡应能看到：

- 原始规则编号；
- 标题和最终候选正文；
- 级别；
- 完整描述；
- 反例；
- 正例；
- 来源文件、章节和行号；
- 检索提示或 checker 信息。

如果只看到单个词，先确认上传的是最新代码，并检查 Markdown 是否确实采用了标题与 `【级别】/【描述】/【反例】/【正例】` 结构。

## 10. 第八步：审批规则

每条规则都有四种决定：

| 决定 | 什么时候使用 |
|---|---|
| 批准并启用 | 抽取结果与原文一致，可以进入正式索引 |
| 修改后接受 | 方向正确，但缺少条件、例外或表述不完整 |
| 拒绝 | 抽到了背景说明、重复内容、反例或错误规则 |
| 暂缓处理 | 暂时无法确认，先不进入正式索引 |

### 10.1 单条审批

1. 展开“查看来源原文与 checker 详情”；
2. 核对级别、描述、反例、正例和来源；
3. 选择决定；
4. 需要时填写审阅备注；
5. 点击“保存本条决定”；
6. 看到“决定已保存”后再处理下一条。

选择“修改后接受”时，必须填写完整最终规则。例如不要只写“补上空值判断”，而应写成：

```text
当传入 Map.of/Map.ofEntries 的 key 或 value 可能为 null 时，
必须先完成空值处理；无法证明非空时不得直接调用。
```

### 10.2 批量批准

如果确认某一批规则都正确：

1. 将“决定状态”筛选为“待处理”；
2. 再按规则编号、严重度、范围或分类缩小结果；
3. 抽查当前结果；
4. 点击“批量批准当前待处理项”；
5. 核对数量并确认。

批量批准只处理当前筛选结果中的待处理规则，不会覆盖已经批准、修改、拒绝或带有未保存修改的规则。

### 10.3 已经确认过的规则

再次生成候选时：

- 内容完全没变：自动保留原决定、修改正文和备注；
- 新增规则：保持待处理；
- 已决定规则发生变化或被删除：页面列出无法继承的决定，并且只在这种情况下要求确认；
- 页面中仍有未保存决定：不允许重新生成候选。

因此，日常新增几条规则时只需要审批新增或变化部分，不需要把全部规则重新选一遍。

## 11. 第九步：激活正式规则库

审批完成后进入“激活规则”。

版本号建议采用：

```text
company-java-2026.09-v1
```

操作步骤：

1. 检查准备批准、待处理、已拒绝数量；
2. 输入不会重复的策略版本号；
3. 点击“确认并激活规则库”；
4. 再次核对弹窗中的数量；
5. 确认激活。

只有“批准并启用”和“修改后接受”的规则会进入正式索引。待处理和拒绝规则不会进入。

激活成功后应生成：

```text
.policy-work/approved-rules.json
.policy-work/search-index.db
.policy-work/GLOBAL_MD_BLOCK.md
```

如果启用了 Embedding，激活提示还会显示向量缓存命中数和新生成数。

## 12. 第十步：在检索沙盒验收

不要激活后马上部署。先在页面“索引检索沙盒”做至少四类测试。

第一次测试先把范围和分类过滤留空，最大返回数设置为 `10` 或 `12`。

### 测试一：变量命名

任务意图：

```text
给订单接口增加一个保存用户名称的局部变量
```

代码片段：

```java
String User_Name = request.getParameter("name");
```

预期命中小驼峰变量命名规则，例如 `G.NAM.01`。

### 测试二：创建顶层 public 类

任务意图：

```text
创建一个顶层 public 订单服务类
```

目标文件：

```text
src/main/java/com/acme/order/OrderService.java
```

代码片段：

```java
public class OrderService {
}
```

预期命中顶层 public 类型 Javadoc 规则，例如 `G.CMT.02`，结果中应显示级别、描述、反例和正例。

### 测试三：外部数据反序列化

任务意图：

```text
从 HTTP 请求体读取并反序列化订单对象
```

代码片段：

```java
Object value = new ObjectInputStream(request.getInputStream()).readObject();
```

预期命中禁止直接反序列化外部数据的规则，例如 `G.SER.01`。

### 测试四：格式化字符串

任务意图：

```text
使用请求参数中的格式模板格式化响应内容
```

代码片段：

```java
String format = request.getParameter("format");
String value = String.format(format, getData());
```

预期命中 `G.EDV.02`。

再测试一个安全写法：

```java
String value = String.format("my format: %s", getData());
```

规则仍可能因为“正在进行格式化操作”而被召回供编码时参考，但确定性检查不应把固定格式模板判定为外部格式串违规。检索“相关”与检查“违规”是两件事。

### 怎样判断检索结果正确

重点检查：

1. 相关规则是否出现在前几名；
2. 是否返回了完整规则，而不是单个词；
3. 规则编号和来源是否正确；
4. 级别、描述、反例和正例是否完整；
5. 已拒绝和待处理规则是否没有出现；
6. 页面显示的是 `SQLITE` 还是 `SQLITE-HYBRID`；
7. 是否存在明显无关结果。

## 13. 第十一步：运行自检

在页面验收完成后，回到项目根目录执行：

```powershell
.\scripts\policy.ps1 doctor
```

至少应确认：

- Python 通过；
- 运行目录可写；
- 已审核规则库通过；
- 检索索引通过；
- AI 和数据库状态符合你的配置。

JDK 或 Maven 未找到时，页面抽取与搜索仍可测试；准备完整接入 Java/Codagent 流程前再补齐。

也可以直接用命令行搜索：

```powershell
.\scripts\policy.ps1 search `
  --query "创建一个顶层 public 订单服务类" `
  --file "src/main/java/com/acme/order/OrderService.java"
```

返回结果应包含规则编号、严重度、来源、级别、描述、反例、正例和命中依据。

## 14. 第十二步：导出并接入 Codagent

只有页面检索和 `doctor` 都通过后再部署。

### 14.1 推荐：导出手工部署包

选择一个长期存在、不会被清理或移动的空目录：

```powershell
.\scripts\export-manual.ps1 `
  -OutputDirectory "D:\company-tools\codagent-java-policy\release-2026-09"
```

如果需要指定 Python：

```powershell
.\scripts\export-manual.ps1 `
  -OutputDirectory "D:\company-tools\codagent-java-policy\release-2026-09" `
  -PythonCommand "C:\Python312\python.exe"
```

目标目录必须为空，脚本不会覆盖已有目录内容。

导出完成后，打开：

```text
D:\company-tools\codagent-java-policy\release-2026-09\COPY_CHECKLIST.md
```

严格按清单完成：

1. 备份 Codagent 现有 Skills、全局 MD 和 Hook；
2. 复制 `java-policy`、`java-review`、`java-policy-authoring` 三个 Skill；
3. 只合并 `CLAUDE_MD_BLOCK.md` 中带标记的内容，不覆盖原全局 MD；
4. 把生成的 Java Policy Hook 条目合并进现有 Hook 配置，不要用整个文件覆盖公司配置；
5. 保持导出包及其中 `runtime` 的绝对路径永久不变；
6. 校验最终 Hook JSON；
7. 重启或重载 Codagent；
8. 用一个真实 Java 小改动验收。

如果以后想移动目录，不要直接剪切原部署包。应在新路径重新导出，因为 Skill 和 Hook 中已经写入绝对路径。

### 14.2 自动安装

只有确认公司 Codagent 兼容 `<CodagentHome>\plugins` 结构时才使用：

```powershell
.\scripts\install.ps1
```

更新已有自动安装：

```powershell
.\scripts\install.ps1 -Update
```

公司魔改版目录或注册方式不确定时，优先使用手工导出。

## 15. 接入后日常怎么用

正常接入 Skill 和 Hook 后，开发人员不需要每次打开 Policy Studio，也不需要手工搜索。

日常流程应当是：

1. 像平时一样给 Codagent 描述开发任务；
2. Codagent 在写入一个方法、类或配置前检索相关规则；
3. 命中时读取完整规则上下文；
4. 没有命中时明确报告“已检索，无专门规范”；
5. 写入后执行能够确定判断的 checker；
6. 会话结束时进行完整变更审查并生成审计报告。

首次 Edit/Write 被 Hook 阻断，同时返回规范上下文时，通常是一次性 receipt 流程：Codagent 应阅读结果后重试写入。若提示 Python 启动失败、索引损坏或 `bundle_id` 不一致，则属于故障，不能直接重试绕过。

查看最近报告：

```powershell
.\scripts\policy.ps1 report
```

自动安装模式查看当前已安装版本报告：

```powershell
.\scripts\policy.ps1 -Installed report
```

## 16. 可选：启用 OpenAI、大模型增强和 Embedding

推荐架构不是“大模型替代解析器”，而是：

```text
固定 Markdown 结构化解析
  + 本地 BM25
  + 代码/API 直接触发
  + 可选 LLM 生成检索别名和场景
  + 可选 Embedding 语义召回
```

### 16.1 启用前先知道两件事

1. 使用远程 OpenAI 或公司网关时，参与增强或查询的规则文本、任务、文件路径和代码片段会发送给对应服务。公司规范和源代码是否允许发送，必须先按公司要求确认。
2. 规则向量按内容缓存，未变化规则不会重复生成；但启用语义检索后，每次新查询仍需要生成一个查询向量。若不希望运行时联网，应接本地 OpenAI-compatible Embedding 服务，或者保持纯 BM25。

### 16.2 配置 API Key

关闭正在运行的 Policy Studio。在准备重新启动 Studio 的同一个 PowerShell 窗口中设置：

```powershell
$env:OPENAI_API_KEY = "你的密钥"
```

不要把真实密钥写进 `policykit.json`，也不要提交到 Git。

### 16.3 修改配置

打开项目根目录的 `policykit.json`，把 `ai` 部分改为：

```json
"ai": {
  "provider": "openai",
  "base_url": "https://api.openai.com/v1",
  "api_key_env": "OPENAI_API_KEY",
  "timeout_seconds": 30,
  "required": false,
  "max_input_chars": 16000,
  "llm": {
    "enabled": true,
    "model": "填写当前账号可用的文本模型",
    "batch_size": 12
  },
  "embedding": {
    "enabled": true,
    "model": "text-embedding-3-small",
    "dimensions": null,
    "batch_size": 64,
    "semantic_weight": 0.4,
    "min_similarity": 0.28
  }
}
```

`model` 不能保留示例文字，必须替换成账号或公司网关实际支持的模型名。

初次调试建议保留 `required: false`。这样 AI 服务暂时不可用时会明确提示并回退到 BM25，不会丢失本地规则。生产环境是否改成 `true`，取决于你是否要求“向量不可用就禁止继续”。

### 16.4 公司网关或本地兼容服务

配置改成：

```json
"provider": "openai-compatible"
```

并在启动 Studio 前设置实际地址和模型：

```powershell
$env:POLICYKIT_OPENAI_BASE_URL = "http://127.0.0.1:8000/v1"
$env:POLICYKIT_LLM_MODEL = "公司文本模型名"
$env:POLICYKIT_EMBEDDING_MODEL = "公司向量模型名"
$env:OPENAI_API_KEY = "公司网关要求的令牌"
```

兼容服务需要提供：

- `POST /responses`；
- `POST /embeddings`。

### 16.5 让配置真正生效

重新启动页面：

```powershell
.\scripts\policy.ps1 ui
```

然后按顺序执行：

1. 重新“生成候选规则”：为新增或变化规则生成检索场景、别名和代码信号；
2. 审批新增或变化规则；
3. 使用新版本号重新激活：为批准规则生成或读取缓存向量；
4. 在检索沙盒查询；
5. 页面索引标记应显示 `SQLITE-HYBRID`。

缓存位置：

```text
.policy-work/ai-enrichment-cache.json
.policy-work/embedding-cache.json
```

新增规则只处理新增内容；规则、模型或维度变化时只重新处理对应缓存未命中部分。

## 17. 可选：连接本地数据库

数据库是激活结果的附加镜像，正式运行时仍以经过 `bundle_id` 校验的 `approved-rules.json` 和 `search-index.db` 为准。

### 17.1 最简单的本地 SQLite

关闭正在运行的 Studio，在 PowerShell 中设置：

```powershell
$env:POLICYKIT_DATABASE_URL = "sqlite:///.policy-work/local-policy.db"
```

打开 `policykit.json`，把 `database` 部分改为：

```json
"database": {
  "enabled": true,
  "adapter": "sqlite",
  "url_env": "POLICYKIT_DATABASE_URL",
  "url": "",
  "required": false,
  "custom_factory": "",
  "options": {}
}
```

重新启动 Studio，再使用一个新版本号执行“确认并激活规则库”。数据库只在激活时同步，不会因为单纯启动页面就生成。

成功后会出现：

```text
.policy-work/local-policy.db
```

验证数据库：

```powershell
python -c "import sqlite3; c=sqlite3.connect(r'.policy-work/local-policy.db'); print(c.execute('select key,value from policykit_metadata order by key').fetchall())"
```

应当能看到 `policy_version`、`bundle_id`、`rule_count` 和 `embedding_count`。完整规则存放在 `policykit_rules` 表。

### 17.2 接 MySQL、PostgreSQL、Milvus 或其他服务

在 `src/company_policy_adapter.py` 中实现：

```python
class CompanyPolicyDatabase:
    def __init__(self, url, options):
        self.url = url
        self.options = options

    def sync_bundle(
        self,
        rules,
        *,
        policy_version,
        bundle_id,
        embeddings,
    ):
        # 在这里建立连接并以事务方式同步规则和向量。
        # rules 中的元素是 PolicyRule，可调用 rule.to_dict()。
        raise NotImplementedError


def create_database(*, url, options):
    return CompanyPolicyDatabase(url, options)
```

配置：

```json
"database": {
  "enabled": true,
  "adapter": "custom",
  "url_env": "POLICYKIT_DATABASE_URL",
  "url": "",
  "required": false,
  "custom_factory": "company_policy_adapter:create_database",
  "options": {}
}
```

然后在 PowerShell 中设置数据库地址，再激活：

```powershell
$env:POLICYKIT_DATABASE_URL = "你的数据库连接地址"
.\scripts\policy.ps1 activate --policy-version "company-java-2026.09-v2"
```

数据库驱动需要安装到 `policy.ps1` 实际使用的 Python 环境中。确认适配器稳定后，如果要求数据库同步失败就禁止激活，再把 `required` 改为 `true`。

## 18. 不使用页面时的命令行完整流程

### 18.1 放入规范

把文件复制到正确范围，例如：

```text
policy-sources/company/Java编码规范.md
policy-sources/company/Java安全规范.md
policy-sources/project/订单项目约定.md
```

### 18.2 生成候选

```powershell
.\scripts\policy.ps1 prepare
```

也可以指定外部目录：

```powershell
.\scripts\policy.ps1 prepare --source "D:\company-java-policies"
```

### 18.3 审批

打开：

```text
.policy-work/REVIEW_ME.md
```

每条规则只能勾选一个决定：

```markdown
- [x] 接受并启用 <!-- decision:approved -->
- [ ] 修改后接受 <!-- decision:modified -->
- [ ] 拒绝 <!-- decision:rejected -->
- [ ] 暂不处理 <!-- decision:pending_review -->
```

### 18.4 激活

```powershell
.\scripts\policy.ps1 activate `
  --review ".policy-work\REVIEW_ME.md" `
  --policy-version "company-java-2026.09-v1"
```

### 18.5 搜索

```powershell
.\scripts\policy.ps1 search `
  --query "使用外部格式模板生成字符串" `
  --file "src/main/java/com/acme/web/OrderController.java"
```

查看 JSON：

```powershell
.\scripts\policy.ps1 search --query "创建顶层 public 类" --json
```

## 19. Linux/macOS 页面试用

PowerShell 脚本主要面向 Windows。只试用 Python CLI 和页面时，可以在项目根目录执行：

```bash
export PYTHONPATH="$PWD/src"
python -m policykit init
python -m policykit prepare
python -m policykit ui
```

激活和搜索：

```bash
python -m policykit activate --policy-version "company-java-2026.09-v1"
python -m policykit search --query "创建顶层 public 类" --json
```

完整导出和 Windows Codagent Hook 生成仍建议在 PowerShell 环境完成。

## 20. 规范更新流程

以后增加或修改规则时，不要编辑 `approved-rules.json` 或 SQLite。

正确流程：

1. 回到源码仓库；
2. 新增 Markdown，或明确替换 `policy-sources` 中的旧版文件；
3. 不要让新旧两份同名规范同时保留；
4. 启动 Studio；
5. 点击“生成候选规则”；
6. 筛选“待处理”，只审批新增或变化规则；
7. 使用新版本号激活；
8. 重跑至少一条应命中、一条不应命中和一条代码触发查询；
9. 运行 `doctor`；
10. 导出到新的空 release 目录并更新 Codagent。

未变化规则的决定会自动保留。只有变化或删除的已决定规则才需要重新确认。

## 21. 常见问题

### PowerShell 提示禁止运行脚本

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

只影响当前窗口，关闭窗口后失效。

### 页面没有自动打开

确认启动命令所在窗口仍在运行，然后手工访问：

```text
http://127.0.0.1:8765/
```

### 上传时提示同名文件

这是防覆盖保护。不要把新版随便改名后与旧版一起导入。先备份旧文件，再明确替换 `policy-sources` 中对应文件。

### 上传成功但候选数为零

检查：

1. 后缀是不是 `.md` 或 `.markdown`；
2. 编码是不是 UTF-8；
3. 文档是否有清晰标题；
4. 文档是否只有目录、图片或链接；
5. 规则正文是否真的写进 Markdown；
6. 是否使用了完整的规范表达，而不是“注意一下”“合理处理”这种模糊文字。

### 为什么重新生成时还有待处理规则

新增规则、正文变化、规则编号变化或已删除后重新加入的规则需要重新确认。内容指纹完全一致的规则会保留决定。

### 激活失败：没有批准规则

至少批准一条规则并保存。系统不会允许用空规则库覆盖当前正式规则。

### 搜索不到规则

按顺序检查：

1. 规则是否已经批准；
2. 是否重新激活；
3. 页面是否显示正式索引可用；
4. 范围和分类过滤是否先留空；
5. 查询是否同时提供了任务、目标文件和代码片段；
6. 规则 ID 是否稳定；
7. `doctor` 是否通过。

### 页面显示 `SQLITE`，没有显示 `SQLITE-HYBRID`

检查：

1. `ai.provider` 是否为 `openai` 或 `openai-compatible`；
2. `ai.embedding.enabled` 是否为 `true`；
3. `OPENAI_API_KEY` 是否在启动 Studio 的进程环境中；
4. Embedding 模型名是否正确；
5. 配置后是否重新激活。

### AI 调用失败

`required: false` 时系统会明确提示并回退到 BM25。检查密钥、Base URL、模型权限以及兼容服务是否实现 `/responses` 和 `/embeddings`。

### 配了数据库但没有生成文件或没有数据

数据库只在成功激活时同步。配置后必须重启 Studio，并使用新版本号重新激活。再运行 `doctor` 检查数据库状态。

### 页面能搜到，Codagent 搜不到

页面使用源码仓库中的当前索引，Codagent 使用上次导出的固定 runtime。重新导出新的 release，并更新 Codagent 的 Skill、全局 MD 和 Hook。

### 提示 `bundle_id` 不一致

不要手改 `approved-rules.json` 或 `search-index.db`。回到审批结果重新执行激活。

## 22. 首次上线检查清单

### 环境

- [ ] `git --version` 正常；
- [ ] `python --version` 不低于 3.10；
- [ ] 完整接入时 JDK 21 和 Maven 可用；
- [ ] 已确认公司 Codagent 的 Skill、全局 MD、Hook 实际位置。

### 规则

- [ ] Markdown 是 UTF-8；
- [ ] 规则编号稳定；
- [ ] 级别、描述、反例、正例抽取完整；
- [ ] 公司、部门、项目范围正确；
- [ ] 没有同时导入新旧版本；
- [ ] 没有把测试规范当作公司规范。

### 审批与检索

- [ ] 已检查候选来源；
- [ ] 已保存所有需要的决定；
- [ ] 至少批准一条规则；
- [ ] 已使用明确版本号激活；
- [ ] 变量命名、顶层类、反序列化、格式化字符串场景符合预期；
- [ ] 已拒绝和暂缓规则不会被检索到；
- [ ] `doctor` 通过。

### Codagent

- [ ] 已备份原有配置；
- [ ] 三个 Skill 已复制；
- [ ] 全局 MD 只合并标记块；
- [ ] Hook 采用合并方式，没有覆盖其他 Hook；
- [ ] 固定 runtime 不会被移动；
- [ ] 已完成一次真实 Java 小改动；
- [ ] 已确认写前检索、写后检查和最终审计报告都正常。

## 23. 最常用命令速查

```powershell
# 初始化
.\scripts\policy.ps1 init

# 启动页面
.\scripts\policy.ps1 ui

# 命令行生成候选
.\scripts\policy.ps1 prepare

# 激活
.\scripts\policy.ps1 activate --policy-version "company-java-2026.09-v1"

# 搜索
.\scripts\policy.ps1 search --query "创建顶层 public 类" --json

# 自检
.\scripts\policy.ps1 doctor

# 导出手工部署包
.\scripts\export-manual.ps1 -OutputDirectory "D:\company-tools\codagent-java-policy\release-2026-09"

# 查看审计报告
.\scripts\policy.ps1 report
```

如果某一步的页面提示与本文不同，先不要跳过。保留 PowerShell 完整输出、页面错误提示、正在使用的 `policykit.json`（删除密钥和数据库密码后）以及对应 Markdown 片段，再进行排查。
