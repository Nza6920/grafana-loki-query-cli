# loki-query

[English](README.md)

通过 Grafana datasource proxy 查询 Loki `query_range` API 的只读 CLI。调用者提供完整 LogQL；CLI 负责 profile、时间范围、鉴权、重试、跨 stream 排序和稳定输出。

## 安装

需要 Python 3.11 或更高版本：

```bash
pipx install .
```

开发时可直接运行：

```bash
PYTHONPATH=src python -m loki_query --help
```

## 配置

复制 [`config.example.toml`](config.example.toml) 到：

```text
${XDG_CONFIG_HOME:-~/.config}/loki-query/config.toml
```

每次查询必须显式指定 profile。profile 仅保存 Token 的环境变量名；不要把 Token 写入 TOML：

```toml
[profiles.prod]
grafana_url = "https://grafana-prod.newchiwan.cn"
datasource_uid = "aPOoJEvIk"
token_env = "GRAFANA_TOKEN"
default_selector = '{namespace="newchiwan-prod"}'
```

```bash
export GRAFANA_TOKEN='...'
loki-query config path
loki-query config validate
loki-query profiles list
```

用 `--config PATH` 临时选择其他配置文件；该全局参数应写在子命令之前。

## 查询

默认查询最近 15 分钟，最多返回 100 条，并按时间全局倒序输出：

```bash
loki-query query \
  --profile prod \
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

输出模式：

- `human`：默认，本地时间和日志正文。
- `raw`：仅日志正文，空结果不输出内容。
- `jsonl`：每行包含 UTC 纳秒时间、全部 labels 和正文，空结果不输出内容。

成功但没有匹配日志时退出码为 `0`。配置或参数错误为 `2`，Token 缺失或认证失败为 `3`，其他 Grafana/Loki 查询失败为 `4`。

CLI 只对 `429`、`502`、`503`、`504` 最多重试两次，并遵循 `Retry-After`。默认请求超时为 30 秒。Token 只从 profile 指定的环境变量读取，不进入命令行或配置文件。

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

```bash
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m mypy src tests
```

生产 smoke test 应使用临时配置、最近 15 分钟和 `limit=1`；验证报告只记录请求是否成功及结果条数，不复述日志正文。
