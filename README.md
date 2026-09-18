# loki-query

[中文文档](README.zh-CN.md)

A read-only CLI for querying Loki's `query_range` API through a Grafana datasource proxy. Callers declare a log or metric query and provide complete LogQL; the CLI handles profiles, time ranges, authentication, retries, global result sorting, and stable output. Version 0.2.0 adds metric range queries and loss-aware output.

## Installation

Python 3.11 or later is required. Install the fixed GitHub release with pipx:

```bash
pipx install "git+https://github.com/Nza6920/grafana-loki-query-cli.git@v0.2.0"
loki-query --version
```

For a local checkout under development, use `pipx install --force .` or run it
directly:

```bash
PYTHONPATH=src python -m loki_query --help
```

Direct source execution supports ordinary commands such as `--help`; install the
project before using `--version`, which reports installed distribution metadata.

## Configuration

On Linux and macOS, copy [`config.example.toml`](config.example.toml) to:

```text
${XDG_CONFIG_HOME:-~/.config}/loki-query/config.toml
```

On Windows, the default path is `%APPDATA%\loki-query\config.toml`.

Every query must explicitly select a profile. A profile stores only the name of the environment variable that contains the token; never put the token itself in the TOML file:

```toml
[profiles.prod]
grafana_url = "https://grafana-prod.newchiwan.cn"
datasource_uid = "aPOoJEvIk"
token_env = "GRAFANA_TOKEN"
default_selector = '{namespace="newchiwan-prod"}'
```

Bash:

```bash
export GRAFANA_TOKEN='...'
loki-query config path
loki-query config validate
loki-query profiles list
```

PowerShell:

```powershell
$configPath = Join-Path $env:APPDATA 'loki-query\config.toml'
New-Item -ItemType Directory -Force (Split-Path $configPath) | Out-Null
Copy-Item .\config.example.toml $configPath
$env:GRAFANA_TOKEN = '...'
loki-query config path
loki-query config validate
loki-query profiles list
```

Configuration lookup order is `--config`, `LOKI_QUERY_CONFIG`,
`XDG_CONFIG_HOME/loki-query/config.toml`, then the platform default above. If
`APPDATA` is unavailable on Windows, the fallback is
`%USERPROFILE%\AppData\Roaming\loki-query\config.toml`. The legacy Windows path
`%USERPROFILE%\.config\loki-query\config.toml` is not searched automatically;
move the file or select it with `LOKI_QUERY_CONFIG`, `XDG_CONFIG_HOME`, or
`--config`. The global `--config` option must appear before the subcommand.
TOML must be UTF-8; when using Windows PowerShell 5.1, preserve the example
file's encoding or use an editor that saves UTF-8.

## Querying

By default, a query is a log query over the last 15 minutes, returns at most 100 entries in backward direction, and outputs them in global reverse chronological order:

```bash
loki-query query \
  --profile prod \
  --query-type log \
  '{namespace="newchiwan-prod"} |= "/customConfig" |= "252143"'
```

Specify a relative time range and JSONL output:

```bash
loki-query query \
  --profile prod \
  --since 2h \
  --limit 50 \
  --output jsonl \
  '{namespace="newchiwan-prod"} |= "252143"'
```

Specify an absolute time range:

```bash
loki-query query \
  --profile prod \
  --start 2026-08-11T08:00:00+08:00 \
  --end 2026-08-11T09:00:00+08:00 \
  '{namespace="newchiwan-prod"} |= "252143"'
```

Read complete LogQL from stdin to avoid complicated shell quoting:

```bash
printf '%s' '{namespace="newchiwan-prod"} |= "252143"' \
  | loki-query query --profile prod --output raw -
```

The equivalent PowerShell pipeline is:

```powershell
'{namespace="newchiwan-prod"} |= "252143"' | loki-query query --profile prod --output raw -
```

Declare metric range queries explicitly and optionally pass a Loki-native step
in seconds or duration form. Metric queries reject the log-only `--limit` and
`--direction` options; log queries reject `--step` before network access:

```bash
loki-query query \
  --profile prod \
  --query-type metric \
  --step 30s \
  --output jsonl \
  'sum(rate({namespace="newchiwan-prod"}[5m]))'
```

Output modes:

- `human`: the default; UTC RFC3339 nanosecond timestamps, labels, and a log line or explicit metric value.
- `raw`: only log lines or original metric value strings; an empty result produces no output.
- `jsonl`: one record per result. Log records use `type: "log_entry"`, `timestamp`, `labels`, and `line`; metric records use `type: "metric_sample"`, `timestamp`, `labels`, and `value`. Empty results produce no output.

A successful query with no matching records exits with status `0`; human output says whether no log entries or metric samples were found. Configuration or argument errors use `2`, a missing token or authentication failure uses `3`, and other Grafana/Loki query failures use `4`.

Successful Loki payloads are parsed defensively. If a response contains malformed
series, log entries, or metric samples, valid records are still emitted, stderr
receives one aggregate warning without record content, and the command exits
with status `4`. If every record is malformed, stdout stays empty.

The CLI retries only `429`, `502`, `503`, and `504` responses, at most twice, and honors `Retry-After`. The default request timeout is 30 seconds. Tokens are read only from the environment variable named by the profile and never enter the command line or configuration file.

## Skill

The repository provides an explicitly invoked `$loki-query` skill at
[`.agents/skills/loki-query/SKILL.md`](.agents/skills/loki-query/SKILL.md):

```text
$loki-query use the prod profile to query error logs for order 252143 from the last 30 minutes
```

Ordinary log discussions do not trigger it automatically.

The skill requires every request to name exactly one profile and uses the
profile's `default_selector` unless the user provides another reliable
selector. It defaults to the last 15 minutes, 100 entries, and JSONL output.
Each CLI query is limited to a 24-hour window. A window beyond one hour must
be explicitly approved in the current request, as must switching profiles.
The five-query limit applies to all CLI queries in one user request, including
the initial query. Results distinguish matching log evidence, inferences, and
unresolved checks while omitting irrelevant production content and
credentials.

To use it in another repository, install or link `.agents/skills/loki-query` into that repository's skills directory and make sure the `loki-query` command has been installed with `pipx`.

## Development checks

Use Python 3.11+ and install the development dependencies in a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m mypy
```

On Windows PowerShell, activate the environment with `.venv\Scripts\Activate.ps1`.
Mypy strictly checks `src` and `tests` using the pinned version and configuration
in `pyproject.toml`. GitHub Actions runs the same check on pushes and pull requests
using Python 3.11.

A production smoke test should use a temporary configuration, the last 15 minutes, and `limit=1`. Its report should record only whether the request succeeded and the number of results, without reproducing log content.
