# Output contract for commands

Commands select an output format through the shared context. The default
`table` format is for interactive use, `json` is machine-readable, and `raw`
prints the command's documented primary value. Prompts and diagnostics go to
stderr so stdout remains parseable for automation.

When adding a command, declare its raw output contract, use the shared error
emitter, and verify `--output table`, `--output json`, and `--output raw` in the
command's tests.
