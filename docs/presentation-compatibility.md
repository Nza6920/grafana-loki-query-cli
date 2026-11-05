# Presentation compatibility

The unified presentation work in #24 preserves existing failure behavior.

For an ordinary scan with `--work-dir`, presentation runs inside the investigation
writer context. That context converts `OSError`, including `BrokenPipeError`
from a closed output stream, into a configuration error (exit 2) with the message
“Cannot access or publish the selected scan work directory.” It does not register
`execution_interrupted` for that failure. Ordinary queries and scans without a
work directory propagate a closed-output exception through command finalization.
Other presentation interruptions, including `KeyboardInterrupt` and `SystemExit`,
propagate for scans with a work directory too, registering `execution_interrupted`.

All these work-directory failures leave the last retrieval checkpoint usable;
output failure before report writing does not commit final presentation evidence.
Changing the writer context or these exit semantics requires a separate decision.

Resume uses the same work-directory writer context for ordinary and trace scans,
so its output `OSError` also becomes exit 2. Offline export has no writer context;
its output exceptions propagate through command finalization, which registers
`execution_interrupted`. A final HTTP accounting line is emitted only when an
HTTP attempt cap was explicitly selected; export has no HTTP accounting line.

Every export clears historical presentation evidence, current parse counters,
and current resource losses on a detached report. Historical retrieval coverage,
counts, reasons, and failure exit status remain visible. Resume instead uses the
scanner's new current counters and losses, retaining historical retrieval counts;
a current parse or resource loss remains a current presentation loss. A prior
output limit alone never causes resolved intervals to be fetched again. Neither
presentation loss nor repeated export with different budgets trims saved records.

Evidence in a written report is captured after record output and diagnostics,
before command finalization. The final stderr evidence may additionally reflect
an interruption or a final status-capacity loss. Metrics retain their existing
renderer and command-level output budget behavior.
