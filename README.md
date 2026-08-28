# loki-query

[中文文档](README.zh-CN.md)

A read-only CLI for querying Loki's `query_range` API through a Grafana datasource proxy. Callers provide complete LogQL queries; the CLI handles profiles, time ranges, authentication, retries, cross-stream sorting, and stable output. Version 0.1.1 adds native Windows configuration paths and PowerShell guidance.

## Installation

Python 3.11 or later is required. Install the fixed GitHub release with pipx:

```bash
pipx install "git+https://github.com/Nza6920/grafana-loki-query-cli.git@v0.1.1"
```

For a local checkout under development, use `pipx install --force .` or run it
directly:

```bash
PYTHONPATH=src python -m loki_query --help
```

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

By default, a query searches the last 15 minutes, returns at most 100 entries, and outputs them in global reverse chronological order:

```bash
loki-query query \
  --profile prod \
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

Output modes:

- `human`: the default; local timestamps and log lines.
- `raw`: log lines only; an empty result produces no output.
- `jsonl`: one line per entry containing a nanosecond UTC timestamp, all labels, and the log line; an empty result produces no output.

A successful query with no matching logs exits with status `0`. Configuration or argument errors use `2`, a missing token or authentication failure uses `3`, and other Grafana/Loki query failures use `4`.

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

```bash
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m mypy src tests
```

A production smoke test should use a temporary configuration, the last 15 minutes, and `limit=1`. Its report should record only whether the request succeeded and the number of results, without reproducing log content.
