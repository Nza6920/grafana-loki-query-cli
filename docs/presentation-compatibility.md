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
