# loki-query

[中文文档](README.zh-CN.md)

A read-only CLI for querying Loki's `query_range` API through a Grafana datasource proxy. Callers declare a log or metric query and provide complete LogQL; the CLI handles profiles, time ranges, authentication, retries, global result sorting, and stable output. Version 0.3.0 adds completeness reports, bounded adaptive scans, saved evidence with resume and offline export, and exact trace timelines.

## Installation

Python 3.11 or later is required. Install the fixed GitHub release with pipx:

```bash
pipx install "git+https://github.com/Nza6920/grafana-loki-query-cli.git@v0.3.0"
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

## Log completeness reports

Every ordinary log query writes one coverage/resource summary to stderr. A
successful response below `--limit` with no parse loss resolves the requested
range, including a valid empty response. Returning the limit warns of **possible
truncation** while retaining exit status `0`; it does not prove records were
omitted. Parse loss preserves usable stdout and returns `4`. Failed requests
leave the range unresolved; authentication still returns `3`. These states
concern retrieval under the selected query and absolute time range, not log
ingestion completeness or proof that a business event did not occur.

Use `--report FILE` to additionally write a versioned JSON report to an explicit
regular file (its parent directory must exist; stdout/devices are rejected):

```bash
loki-query query --profile prod --since 30m --limit 1000 \
  --output jsonl --report investigation-report.json \
  '{namespace="newchiwan-prod"}' > evidence.jsonl
```

Report schema version `1` contains the profile, actual endpoint/datasource,
query, absolute nanosecond bounds (decimal strings), requested fetch direction,
`status` (`complete`, `incomplete`, or `failed`), and `coverage.resolved` /
`coverage.unresolved` intervals. Unresolved intervals carry `reasons`, including
`possible_truncation`, `parse_loss`, `request_failed`, or
`authentication_failed`. `counts` separates raw returned entries, valid
records, skipped entries, and skipped series. Raw entries include malformed
records and entries in invalid-label series; `raw_count_exact: false` means a
malformed container or failed response prevents an exact count.

`request_limit`, `usage.current`, `usage.cumulative`, `budgets`, and
`stop_reasons` expose request limits, actual HTTP attempts (including retries),
elapsed seconds (including waits), the effective attempt maximum (three, or a
smaller explicit HTTP cap) and per-request timeout. Ordinary queries have no aggregate elapsed/record budget
(the corresponding values are `null`), and current/cumulative usage is equal.
A report is written on success and expected request failure, once the target
and range are resolved. Configuration/input validation failures may occur
before a report can be produced. Reports contain no log records or token
values; credentials remain in the environment.

Without `--report` no JSON report is written. Stdout remains exclusively the
selected evidence format, with unchanged JSONL records, raw lines, and global
descending presentation even for `--direction forward`. An ordinary query
still issues one logical range request with existing retries; it never
silently scans additional intervals. `--report` is log-only; metric behavior
and output remain unchanged.

## HTTP attempt allowance

An explicit `--max-http-attempts N` caps actual HTTP attempts, including failed
requests and retries, on `query` (log or metric), `trace`, scans, and `resume`.
It stops before the next request or retry wait when exhausted, returning query
failure/incomplete status `4`; authentication retains status `3`. Applicable
log completeness reports use `http_attempt_budget` for this stop. Ordinary and
trace queries still make one logical request with at most three attempts;
a larger cap adds no retries. Without an explicit cap their behavior remains
unchanged. Scans and resumes keep their standalone default cap of 60.
Metric queries continue to reject completeness `--report` and scan-only flags.

When the cap is explicitly supplied, exactly one final scalar diagnostic is
written to stderr, including on handled errors and interruption:

```text
HTTP attempt accounting: used=2 cap=57
```

This exact grammar is a stable accounting contract: `used` and `cap` are decimal
integers; `used` counts attempts begun by this process, including failed or
interrupted attempts. It is separate from completeness JSON and never enters
stdout. Pre-network validation errors report zero once arguments are parsed;
argument parsing failures or forced termination may leave no marker. Output or
report failures still finalize accounting when stderr remains writable. Offline
`export` performs no HTTP and emits no marker. A consumer must accept only one
final marker from the current invocation with matching cap and `0 <= used <= cap`;
otherwise conservatively charge the whole passed cap. For resumes use this
current-process count, not cumulative saved usage.

## Bounded interval scans

Add `--scan` explicitly to a log query or trace investigation. The complete LogQL, profile,
and time scope are retained. Relative times are frozen once; RFC 3339 bounds
preserve nanosecond precision, including sub-second ranges. The scan uses
contiguous half-open intervals of at most 15 minutes, with a shorter final
interval. Requests run serially, newest interval first by default or oldest
first with `--direction forward`; generic logs display descending and trace timelines ascending.

```bash
loki-query query --profile prod --since 1h --scan \
  --max-http-attempts 60 --max-elapsed-seconds 300 --max-records 10000 \
  --output jsonl --report scan-report.json '{namespace="prod",app="api"}'
```

Scan defaults are `--limit 1000` per request, 60 actual HTTP attempts including
retries, 300 seconds including waits, and 10000 retained records. Override
these using `--limit`, `--max-http-attempts`, `--max-elapsed-seconds`, and
`--max-records`; budgets must be positive and finite. `--max-elapsed-seconds`
and `--max-records` require `--scan`; scan-only options reject metric queries
before networking. `--max-http-attempts` also applies to ordinary queries and
trace, as described above.
Ordinary queries and trace lookups retain their default limit of 100 and existing behavior.

The remaining elapsed allowance constrains every HTTP timeout. A retry wait
that would consume the remaining allowance stops the scan without sleeping;
a retry never sleeps after exhausting the attempt budget. There are at most
two retries per interval for retryable HTTP responses, with `Retry-After`
respected for 429. The remaining record allowance also caps the next request's
limit; even an oversized response cannot exceed retained capacity. Identical
records remain separate evidence, and actual raw network counts are reported
separately from retained valid records. Each coverage interval records its
actual attempt count and requested limit (null for unqueried work).

A saturated response (raw returned count reaches its request limit) or timeout
bisects that interval without rewriting the query. Child bounds remain exact,
contiguous integer nanoseconds, including odd-duration splits; direction also
controls which child runs first. At or below one second, subdivision stops and
the unresolved interval reports `minimum_interval` together with
`possible_truncation` or `timeout`. HTTP 429 uses bounded backoff and does not
cause subdivision. Parse losses and exhausted temporary failures preserve
valid evidence and allow independent intervals to continue.

A resolved child replaces only its own portion of the parent sample. Unqueried
or failed children keep usable inherited records, and incomplete replacements
within the same execution keep the single sample with more valid records rather
than combining observations. Genuine identical records remain separate.
Replacement reuses the record capacity
occupied by that interval, so parent and child network results do not count
twice against the retained-record budget. Coverage includes `retained_records`
and `sample_origin`; `requests` records queried parent and child bounds,
limits, attempts and outcomes, while aggregate raw counts describe actual
network results and valid counts describe the final retained evidence.

Authentication and invalid-query failures stop immediately, retaining earlier
evidence and reporting remaining work. Reports identify `http_attempt_budget`, `elapsed_time_budget`, and
`record_budget` stops. Unresolved scans return 4, authentication returns 3,
and configuration errors return 2. Without `--work-dir`, scans do not write log
archives; an explicit `--report` writes only completeness metadata.

## Saved scan evidence and offline export

Opt into durable progress and captured candidate evidence with `query --scan --work-dir`
or `trace --scan --work-dir`:

```bash
loki-query query --profile prod --since 1h --scan --work-dir incident-work \
  --output jsonl '{namespace="prod",app="api"}'
loki-query export --work-dir incident-work --output jsonl --report saved-report.json
```

Use a new, dedicated directory for each investigation. The versioned checkpoint
binds profile, actual endpoint/datasource, complete LogQL, frozen absolute
nanosecond bounds, fetch direction and result schema. It stores terminal interval
states, sample provenance, original log lines/labels and multiplicity, request
history and execution accounting. Authentication headers and token values are
never included as credentials. Captured log contents remain original evidence.

Each checkpoint contains its evidence and completion state in one checksummed
file, flushed and atomically replaced. Progress is committed before each HTTP
attempt and after interval transitions, so a stopped process retains completed
intervals and conservatively charges an in-flight attempt. Interrupted publication
leaves the preceding committed checkpoint usable; incomplete temporary files do
not count as completed work. An OS writer lock rejects concurrent scans and is
released automatically if the process dies. Corrupt, missing or unsupported state
is rejected rather than treated as a completed scan. The state file and lock are
reserved and cannot be selected as a report destination.

`export` requires no configuration, token or HTTP access. It reconstructs retained
interval evidence in generic-query descending order or trace ascending order, without combining parent
and child observations or deduplicating genuine repeats. Its stderr summary and
optional JSON report carry the saved coverage, gap reasons and usage; JSONL stays
evidence-only. Partial scans still return 4, and saved authentication failures
return 3. Export does not refresh logs or backfill later arrivals. The saved
intervals are observations from separate requests, not a server-side snapshot.

## Resume an investigation

Continue only the saved unresolved terminal intervals with an explicit original
profile and work directory:

```bash
loki-query resume --profile prod --work-dir incident-work \
  --max-http-attempts 20 --max-records 2000 --output jsonl --report resumed-report.json
```

Resume validates the saved schema, profile and actual endpoint/datasource before
HTTP or token lookup, and reuses the original complete LogQL, absolute nanosecond
bounds and fetch direction. Relative time expressions are never reevaluated.
Changed scope requires a new investigation; `resume` accepts no query, range or
direction flags. Current credentials are loaded from the selected profile's
current token environment variable, so both token values and the environment
variable name can rotate without storing credentials. An already complete scan
makes no requests and does not require a token. Generic log and all trace source
modes use the same scan engine and checkpoint validation.

Each execution receives fresh request, elapsed and retained-record allowances.
`--limit`, `--timeout`, execution budgets and output/report choices may change.
Previously saved evidence does not consume the new record allowance, so merged
exports can contain more rows than the current `--max-records`. The report's
`usage.current` tracks this execution's attempts, elapsed time, raw/skipped
observations and charged retained records; `usage.cumulative` tracks total
attempts, elapsed time and observations, plus the final merged retained-record
count. Top-level `counts` describes cumulative observations and merged evidence.
A reserved HTTP attempt is checkpointed before dispatch and conservatively counts
if the process stops before the response. Repeated checkpoints never re-add an
execution's usage to cumulative totals.

A new response, including a partial response, replaces the unresolved interval's
saved sample even when it contains fewer valid records; an empty response removes
its old rows. Parse losses still leave the interval incomplete. Failed requests
preserve saved evidence. Completed intervals stay frozen and are never implicitly
queried again. They are observations gathered at different times, not a server
snapshot: refreshing them or including late arrivals requires a fresh
investigation. Resume uses the same atomic publication and exclusive writer lock
as initial scans; offline export remains available after interruption.

## Trace timelines

Use an explicitly selected profile, a single stream selector, and an explicit
relative or absolute time boundary:

```bash
loki-query trace --profile uat --selector '{namespace="apps",app=~"api|worker"}' \
  --since 15m --report trace-report.json 'your-trace-id'
```

The default JSON-body mode checks top-level `trace_id` or `traceId` for complete,
case-sensitive **string** equality. It retrieves candidates using your unchanged
selector, then checks the original JSON locally. This preserves selector
constraints, log lines, and all returned labels, including colliding field names.
In JSON-body mode the trace ID is never interpolated into LogQL. Numeric values, absent fields,
non-JSON bodies, duplicate JSON keys, and nonstandard JSON constants are rejected;
these expected nonmatches are not API parse losses. There is no label or full-text
fallback. Generic `query` remains available for caller-authored full-text searches.
A pipeline or expression after `--selector` is rejected before network access.

In JSON-body mode, use repeated `--field NAME` for literal top-level JSON keys, or repeated
`--path POINTER` for nested JSON object members. The combined custom list
**replaces** both defaults; any selected field/path may match. For example:

```bash
loki-query trace --profile uat --selector '{namespace="apps",app="api"}' \
  --since 15m --field 'trace.id' --path '/context/traceId' 'your-trace-id'
```

Here `trace.id` is one literal key, while `/context/traceId` traverses two object
members. Paths use JSON Pointer escaping: `~1` means `/`, and `~0` means `~`;
`/a~1b/~0key` selects the member `~key` inside `a/b`. Members must be nonempty
and contain no control characters. Empty names, invalid escapes and paths
without an initial `/` are rejected before network access. Paths traverse
objects only; there is no array indexing, wildcard, dotted-path, or JSONPath
interpretation. Literal field names may contain punctuation, quotes and spaces,
but must be nonempty and contain no control characters. All names and IDs remain
local data rather than generated LogQL. Missing/non-string values never fall
back to defaults. Reports include selected `fields`, `paths`, and a sorted,
deduplicated `mapping` of decoded object-member components for query identity.

Select label matching explicitly when the ID is stored in returned labels rather
than the body. Label mode requires one or more explicit `--field` names:

```bash
loki-query trace --profile uat --selector '{namespace="apps",app=~"api|worker"}' \
  --since 15m --source label --field trace_id --field correlation_id 'your-trace-id'
```

`--source json` is the default; each query uses only its selected source. Label
mode checks complete, case-sensitive label strings, including structured metadata
exposed in the normal returned label map. It appends an exact OR label filter to
your selector with safely quoted IDs, then checks the returned labels locally.
Field names must be ASCII identifiers (`[A-Za-z_][A-Za-z0-9_]*`); JSON `--path`
options are rejected before network access. The body need not contain the ID or
even be JSON. No JSON-body fallback or source union occurs. Original lines and
labels remain intact, and the report records source, fields, canonical mapping,
original selector, and generated query. Field order and duplicates do not change
the label investigation identity. Missing or conflicting body values cannot
confirm a label match. Categorized-label response formats are not enabled.

The human timeline is ascending at nanosecond precision. It displays `app` as
service and retains namespace and every returned label. Use `--service-field NAME`
to choose another returned label; absent values display `unknown`. Equal timestamps
have deterministic label/text ordering without implying causality, and repeated
identical records remain repeated. `--output raw` preserves original lines;
`--output jsonl` preserves the existing `log_entry` schema without summary rows.

Without `--scan`, this command performs one bounded logical request (including existing HTTP
retries), with `--limit 100` and `--direction backward` by default. Fetch direction
does not change chronological presentation. Completeness and limit saturation
are computed from **raw candidates**, even when zero candidates match the ID.
The separate report retains candidate counts and adds `matched_records`,
`filtered_candidates`, and the exact trace identity. Stderr qualifies any no-match
result by the selected conditions and candidate completeness. Saturation alone
keeps exit status 0; API parse losses retain evidence and return 4. Neither a
complete retrieval nor no matches proves ingestion completeness or event absence.

### Scan, save, resume and export a timeline

Every trace source supports the same opt-in `--scan`: default JSON fields, custom
literal fields/JSON Pointers, and explicit labels. The shared engine handles
15-minute intervals, adaptive subdivision, retries, coverage and all budgets.
Its default request limit is 1000; a trace lookup without scanning stays at 100.
Fetch direction never changes ascending human, raw or JSONL presentation.

```bash
loki-query trace --profile uat --selector '{namespace="apps",app=~"api|worker"}' \
  --since 30m --scan --work-dir trace-work --max-http-attempts 20 \
  --max-records 2000 --output jsonl --report trace-scan.json 'your-trace-id'
loki-query resume --profile uat --work-dir trace-work --trace-id 'your-trace-id' \
  --max-http-attempts 10 --service-field service_name --output human
loki-query export --work-dir trace-work --output jsonl --report trace-export.json
```

Trace checkpoints retain original **candidate** lines and labels, the exact ID,
source, original selector, canonical field mapping and actual server query.
Coverage, saturation, subdivision and record allowances use candidates before
local matching: even zero confirmed matches can exhaust the record budget and
leave an incomplete timeline. Reports distinguish cumulative network observations,
retained candidates and matched records; rejected trace predicates are not API
parse losses. Parent-to-child sample replacement preserves genuine duplicates.

Resume validates trace identity and query consistency before token lookup or
HTTP; export performs the same semantic validation without configuration or
credentials. Resume accepts optional `--trace-id`, `--source`, `--selector`,
`--field` and `--path` assertions; they must match the frozen investigation.
Equivalent canonical mappings may reorder or repeat fields. Changing the ID,
source, selector or mapping requires a new work directory. Output format,
`--service-field`, timeout and execution budgets may change. Only unresolved
intervals are fetched; completed evidence remains frozen. Every checkpoint,
including an interrupted first request, carries the trace identity and coverage.

Offline export applies the saved predicate to saved candidates and displays the
same chronological evidence, including repeated rows and original labels/lines.
An incomplete archive remains incomplete on export. A complete selected-range
retrieval only establishes the matching subset of those observed candidates;
it does not establish a full request lifecycle, causality, ingestion completeness
or a server snapshot. Later arrivals require a fresh investigation.

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
the initial query, scans, trace queries, and resumes, even on failure. A shared
request allowance defaults to 60 actual HTTP attempts including retries. The
skill passes only the remaining amount as `--max-http-attempts`, settles the
final accounting marker after every outcome, and charges the full reserved cap
if trustworthy accounting is missing. Restarting the CLI or changing work
directories cannot reset the ledger. Only explicit user direction can increase
the request-wide total (raising 60 to 90 after charging 60 leaves 30); this does
not waive the five-query limit. Offline export remains available at exhaustion.
Results distinguish returned evidence, inferences, unresolved checks, actual
usage, and conservative charges while omitting irrelevant production content
and credentials.

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
in `pyproject.toml`. GitHub Actions runs the unit tests, compilation checks and
the same strict type check on pushes and pull requests using Python 3.11.

A production smoke test should use a temporary configuration, the last 15 minutes, and `limit=1`. Its report should record only whether the request succeeded and the number of results, without reproducing log content.
