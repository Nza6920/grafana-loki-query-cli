---
name: loki-query
description: Query Grafana Loki logs through the repository CLI.
disable-model-invocation: true
---

# Loki query

Use the installed `loki-query` CLI for read-only log investigation.

1. Require the user to name a profile. If it is missing, ask for it and stop. Complete this step when exactly one profile is selected.
2. Run `loki-query config path`, read that TOML file, and use the selected profile's optional `default_selector` as query context. If no reliable selector is available from the profile or user, ask for one and stop. Never read or print the environment variable named by `token_env`. Complete this step when a complete LogQL query can be written without guessing the environment.
3. Choose the narrowest useful time window. Default to `--since 15m`, `--limit 100`, and `--output jsonl`. Show the profile, time window, and LogQL in a commentary update, then run one query. Complete this step when the CLI returns results or a handled error.
4. Follow identifiers in returned logs and refine the LogQL when that can answer the user's question. Use at most five CLI queries per user request. State the reason for every follow-up query. Complete this step when the evidence answers the question or the remaining gap is identified.
5. Ask before switching profiles or widening beyond one hour. Never widen beyond 24 hours. Complete this step when the user approves the change or the investigation remains within its current boundary.
6. Report matching log evidence, the resulting inference, and unresolved checks separately. Omit irrelevant production log content and credentials. Complete this step when every conclusion is traceable to returned JSONL entries.

Treat an empty result as a successful query with no matches. For current syntax and options, run `loki-query --help` and `loki-query query --help`; keep those commands as the source of truth.
