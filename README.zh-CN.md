# loki-query

[English](README.md)

通过 Grafana datasource proxy 查询 Loki `query_range` API 的只读 CLI。调用者声明日志或指标查询并提供完整 LogQL；CLI 负责 profile、时间范围、鉴权、重试、全局结果排序和稳定输出。0.2.0 版本增加指标范围查询和损坏数据感知输出。

## 安装

需要 Python 3.11 或更高版本。使用 pipx 安装固定的 GitHub release：

```bash
pipx install "git+https://github.com/Nza6920/grafana-loki-query-cli.git@v0.2.0"
loki-query --version
```

开发中的本地 checkout 可使用 `pipx install --force .`，或直接运行：

```bash
PYTHONPATH=src python -m loki_query --help
```

直接从源码运行时可使用 `--help` 等普通命令；`--version` 报告已安装发行版的
元数据，因此使用前需要先安装项目。

## 配置

在 Linux 和 macOS 上，将 [`config.example.toml`](config.example.toml) 复制到：

```text
${XDG_CONFIG_HOME:-~/.config}/loki-query/config.toml
```

Windows 默认位置为 `%APPDATA%\loki-query\config.toml`。

每次查询必须显式指定 profile。profile 仅保存 Token 的环境变量名；不要把 Token 写入 TOML：

```toml
[profiles.prod]
grafana_url = "https://grafana-prod.newchiwan.cn"
datasource_uid = "aPOoJEvIk"
token_env = "GRAFANA_TOKEN"
default_selector = '{namespace="newchiwan-prod"}'
```

Bash：

```bash
export GRAFANA_TOKEN='...'
loki-query config path
loki-query config validate
loki-query profiles list
```

PowerShell：

```powershell
$configPath = Join-Path $env:APPDATA 'loki-query\config.toml'
New-Item -ItemType Directory -Force (Split-Path $configPath) | Out-Null
Copy-Item .\config.example.toml $configPath
$env:GRAFANA_TOKEN = '...'
loki-query config path
loki-query config validate
loki-query profiles list
```

配置查找顺序为 `--config`、`LOKI_QUERY_CONFIG`、
`XDG_CONFIG_HOME/loki-query/config.toml`，最后是上述平台默认位置。Windows 缺少
`APPDATA` 时回退到 `%USERPROFILE%\AppData\Roaming\loki-query\config.toml`。不会
自动查找旧的 Windows 路径 `%USERPROFILE%\.config\loki-query\config.toml`；请移动
文件，或通过 `LOKI_QUERY_CONFIG`、`XDG_CONFIG_HOME`、`--config` 显式选择。
全局参数 `--config` 应写在子命令之前。TOML 必须使用 UTF-8；使用 Windows
PowerShell 5.1 时，请保留示例文件编码，或使用可将文件保存为 UTF-8 的编辑器。

## 查询

默认执行最近 15 分钟的日志查询，按 backward 方向最多返回 100 条，并按时间全局倒序输出：

```bash
loki-query query \
  --profile prod \
  --query-type log \
  '{namespace="newchiwan-prod"} |= "/customConfig" |= "252143"'
```

指定相对时间和 JSONL 输出：

```bash
loki-query query \
  --profile prod \
  --since 2h \
  --limit 50 \
  --output jsonl \
  '{namespace="newchiwan-prod"} |= "252143"'
```

指定绝对时间：

```bash
loki-query query \
  --profile prod \
  --start 2026-08-11T08:00:00+08:00 \
  --end 2026-08-11T09:00:00+08:00 \
  '{namespace="newchiwan-prod"} |= "252143"'
```

从 stdin 读取完整 LogQL，避免复杂的 shell 引号：

```bash
printf '%s' '{namespace="newchiwan-prod"} |= "252143"' \
  | loki-query query --profile prod --output raw -
```

对应的 PowerShell pipeline 为：

```powershell
'{namespace="newchiwan-prod"} |= "252143"' | loki-query query --profile prod --output raw -
```

指标范围查询必须显式声明，并可使用秒数或 duration 形式的 Loki 原生 step。
指标查询会在联网前拒绝日志专用的 `--limit` 和 `--direction`；日志查询则会拒绝
`--step`：

```bash
loki-query query \
  --profile prod \
  --query-type metric \
  --step 30s \
  --output jsonl \
  'sum(rate({namespace="newchiwan-prod"}[5m]))'
```

输出模式：

- `human`：默认，输出 UTC RFC3339 纳秒时间、labels，以及日志正文或显式指标值。
- `raw`：仅输出日志正文或指标原始字符串值；空结果不输出内容。
- `jsonl`：每个结果一行。日志记录包含 `type: "log_entry"`、`timestamp`、`labels`、`line`；指标记录包含 `type: "metric_sample"`、`timestamp`、`labels`、`value`。空结果不输出内容。

成功但没有匹配记录时退出码为 `0`；human 输出会区分没有日志条目还是没有指标样本。配置或参数错误为 `2`，Token 缺失或认证失败为 `3`，其他 Grafana/Loki 查询失败为 `4`。

CLI 会防御性解析 Loki 成功响应。响应中若有损坏的 series、日志条目或指标样本，
仍会输出有效记录，stderr 只输出一条不含记录内容的聚合警告，并以状态 `4` 退出。
若所有记录都损坏，stdout 保持为空。

CLI 只对 `429`、`502`、`503`、`504` 最多重试两次，并遵循 `Retry-After`。默认请求超时为 30 秒。Token 只从 profile 指定的环境变量读取，不进入命令行或配置文件。

## 日志完整性报告

每次普通日志查询都会向 stderr 写入一条覆盖范围及资源摘要。成功响应的原始条目数
低于 `--limit` 且没有解析损失时，请求范围视为已解决，合法的空响应也包括在内。
达到 limit 会提示**可能截断**，但仍返回 `0`，这并不证明实际遗漏了记录。
解析损失会保留可用 stdout 并返回 `4`；请求失败会留下未解决范围，鉴权失败仍返回
`3`。这些状态只描述给定查询和绝对时间范围内的检索情况，不证明日志采集完整性，
也不证明某个业务事件没有发生。

通过 `--report FILE` 将带版本号的 JSON 报告写入显式指定的普通文件（父目录必须
已经存在，不接受 stdout 或设备文件）：

```bash
loki-query query --profile prod --since 30m --limit 1000 \
  --output jsonl --report investigation-report.json \
  '{namespace="newchiwan-prod"}' > evidence.jsonl
```

报告 schema 版本为 `1`，包含 profile、实际 endpoint/datasource、查询、绝对纳秒
边界（十进制字符串）、请求的获取方向、`status`（`complete`、`incomplete` 或
`failed`），以及 `coverage.resolved` / `coverage.unresolved` 区间。未解决区间的
`reasons` 包括 `possible_truncation`、`parse_loss`、`request_failed` 或
`authentication_failed`。`counts` 分开统计原始条目、有效记录、跳过的条目及
series；原始条目包括损坏的记录及标签无效的 series 中的条目。
`raw_count_exact: false` 表示容器结构损坏或响应失败，无法精确统计原始条目数。

`request_limit`、`usage.current`、`usage.cumulative`、`budgets` 和 `stop_reasons`
记录请求 limit、包含重试的实际 HTTP 尝试次数、包含等待的耗时秒数、现有最多三次
尝试限制及单次请求超时。普通查询没有累计耗时或记录数预算（对应字段为 `null`），
本轮与累计用量相同。目标和范围解析完成后，成功和预期请求失败都会写入报告；配置或
输入校验失败可能发生在能够生成报告之前。报告不包含日志记录或 token 值，凭证仍只
保留在环境变量中。

未指定 `--report` 时不写 JSON 报告。Stdout 只保留所选证据格式，JSONL 记录、raw
原文及全局降序展示不变，即使使用 `--direction forward` 也是如此。普通查询仍只有
一个逻辑范围请求及原有重试，不会隐式扫描更多区间。`--report` 仅适用于日志查询；
指标查询的行为和输出保持不变。

## 有界分段扫描

显式添加 `--scan` 才会扫描日志查询的时间范围。完整 LogQL、profile 和查询范围
保持不变。相对时间只解析一次；RFC 3339 边界保留纳秒精度，支持不足一秒的范围。
扫描按最长 15 分钟的连续半开区间切分，最后一个区间可以更短。请求串行执行，
默认优先查询最新区间，`--direction forward` 则优先最早区间；日志展示始终全局降序。

```bash
loki-query query --profile prod --since 1h --scan \
  --max-http-attempts 60 --max-elapsed-seconds 300 --max-records 10000 \
  --output jsonl --report scan-report.json '{namespace="prod",app="api"}'
```

扫描默认每次请求 `--limit 1000`，最多 60 次实际 HTTP 尝试（含重试）、300 秒
（含等待），并最多保留 10000 条记录。可通过 `--limit`、`--max-http-attempts`、
`--max-elapsed-seconds` 和 `--max-records` 调整；预算必须是有限正数。后三个预算
参数要求同时启用 `--scan`，所有扫描参数与指标查询组合时都会在联网前拒绝。
普通查询仍默认 limit 为 100，保持原有行为。

每次 HTTP 超时都受剩余时间预算约束。若重试等待会耗尽剩余时间，扫描直接停止，
不执行等待；尝试次数耗尽后也不会等待重试。可重试 HTTP 响应每个区间最多重试
两次，429 遵守 `Retry-After`。剩余记录额度会限制下次请求的 limit；即使响应
超额返回，也不会超过保留容量。相同记录仍分别保留，报告区分网络返回原始条数和
保留有效条数。每个覆盖区间记录实际尝试次数和请求 limit，未查询区间的 limit 为 null。

在本阶段的固定区间扫描中，饱和响应和超时会留下明确缺口。解析损失和重试耗尽的
临时失败会保留有效证据，并继续处理独立区间。认证失败和无效查询立即停止，保留
此前证据并报告剩余工作。报告用 `http_attempt_budget`、`elapsed_time_budget`、
`record_budget` 标明预算停止原因。扫描存在未解决区间时退出码为 4，认证失败为 3，
配置错误为 2。扫描不会写入日志档案；只有显式 `--report` 会写入完整性元数据。

## Trace 时间线

必须显式选择 profile、单个 stream selector，以及相对或绝对时间边界：

```bash
loki-query trace --profile uat --selector '{namespace="apps",app=~"api|worker"}' \
  --since 15m --report trace-report.json 'your-trace-id'
```

默认 JSON body 模式只检查顶层 `trace_id` 或 `traceId`，要求完整、区分大小写的
**字符串**相等。CLI 使用原样 selector 获取候选记录，再在本地解析原始 JSON 并
精确匹配，保留 selector 约束、原始日志及所有返回标签，包括与字段同名的标签。
JSON body 模式的 Trace ID 不会插入 LogQL。数字、字段缺失、非 JSON、重复 JSON 键和非标准 JSON
常量都不确认匹配；这些正常的不匹配不计入 API 解析损失。不会回退到标签或全文
匹配；独立的 `query` 仍支持调用者编写全文查询。`--selector` 后的 pipeline 或
表达式会在联网前被拒绝。

JSON body 模式中，重复传入 `--field NAME` 可选择顶层 JSON 的字面键名，重复传入 `--path POINTER`
可选择嵌套 JSON 对象成员。合并后的自定义列表**替换**两个默认字段，任一所选
字段或路径匹配即可。例如：

```bash
loki-query trace --profile uat --selector '{namespace="apps",app="api"}' \
  --since 15m --field 'trace.id' --path '/context/traceId' 'your-trace-id'
```

其中 `trace.id` 是单个字面键，`/context/traceId` 则依次访问两层对象成员。
路径使用 JSON Pointer 转义：`~1` 代表 `/`，`~0` 代表 `~`；
`/a~1b/~0key` 选择 `a/b` 下的 `~key`。成员名必须非空且不含控制字符。
空名称、无效转义及不以 `/` 开头的路径在联网前拒绝。路径只遍历对象，
不解释数组下标、通配符、点分路径或 JSONPath。字面字段名可包含标点、引号及
空格，但必须非空且不含控制字符。名称和 ID 均只作为本地数据，不生成 LogQL。
字段缺失或值非字符串时不会回退到默认字段。报告包含所选 `fields`、`paths`，
以及排序、去重的解码后对象成员 `mapping`，用于标识查询身份。

ID 位于返回标签时，必须显式选择标签来源，并提供一个或多个 `--field`：

```bash
loki-query trace --profile uat --selector '{namespace="apps",app=~"api|worker"}' \
  --since 15m --source label --field trace_id --field correlation_id 'your-trace-id'
```

默认是 `--source json`；每次查询只使用选定的来源。标签模式对完整标签字符串进行
区分大小写的精确匹配，包括普通返回标签映射中暴露的 structured metadata。
CLI 在原始 selector 后追加精确 OR 标签过滤器，安全引用 ID，再在本地核对返回标签。
字段名必须是 ASCII 标识符（`[A-Za-z_][A-Za-z0-9_]*`）；JSON `--path` 选项会在
联网前被拒绝。正文不必包含 ID，也不必是 JSON；不会回退到正文或合并两个来源。
原始日志及标签完整保留，报告记录来源、字段、规范化映射、原始 selector 和生成查询。
字段顺序及重复字段不改变标签调查身份。正文缺失或冲突的值不能确认标签匹配，
不会启用 categorized-label 响应格式。

人类可读时间线按纳秒时间戳升序排列，默认用 `app` 展示服务，同时保留 namespace
及所有返回标签。`--service-field NAME` 可指定其他返回标签，缺失时显示 `unknown`。
相同时间戳按标签及文本确定显示顺序，不推断因果；真实重复记录仍重复保留。
`--output raw` 保留原始文本，`--output jsonl` 保持现有 `log_entry` schema，
不混入摘要行。

命令执行一次有界逻辑请求（包含现有 HTTP 重试），默认 `--limit 100`、
`--direction backward`；获取方向不改变时间线升序展示。完整性及 limit 饱和均按
**原始候选记录**计算，即使没有候选匹配 ID。独立报告保留候选计数，增加
`matched_records`、`filtered_candidates` 及精确 trace 身份。stderr 的无匹配提示
限定为所选条件，并声明候选检索是否完整。仅饱和仍返回 0；API 解析损失保留有效
证据并返回 4。完整检索及无匹配均不证明日志采集完整性或业务事件不存在。

## Skill

仓库内显式调用的 `$loki-query` skill 位于
[`.agents/skills/loki-query/SKILL.md`](.agents/skills/loki-query/SKILL.md)：

```text
$loki-query 使用 prod profile 查询最近 30 分钟内订单 252143 的异常日志
```

它不会因普通的日志讨论自动触发。每个请求必须明确指定一个 profile；除非用户提供其他可靠 selector，否则使用该 profile 的 `default_selector`。默认查询最近 15 分钟、最多 100 条，并输出 JSONL。

每次 CLI 查询的时间窗不得超过 24 小时。查询超过一小时的时间窗必须在当前请求中得到明确授权，切换 profile 也必须再次确认。每个用户请求最多执行五次 CLI 查询，该限制包含首次查询。结果会分别说明匹配的日志证据、由此得到的推断和仍待核实的事项，并省略无关的生产日志内容和凭据。

如需在其他仓库使用，将 `.agents/skills/loki-query` 安装或链接到相应仓库的 skills 目录，并确保 `loki-query` 命令已通过 `pipx` 安装。

## 开发验证

使用 Python 3.11+，在虚拟环境中安装开发依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m mypy
```

Windows PowerShell 使用 `.venv\Scripts\Activate.ps1` 激活虚拟环境。Mypy 使用
`pyproject.toml` 中固定的版本和配置严格检查 `src` 与 `tests`。GitHub Actions
在推送和 pull request 时使用 Python 3.11 运行同一检查。

生产 smoke test 应使用临时配置、最近 15 分钟和 `limit=1`；验证报告只记录请求是否成功及结果条数，不复述日志正文。
