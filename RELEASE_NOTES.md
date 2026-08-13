# Release notes — draft

Content for the first release. Not published yet.

## What this is

One CLI for the Unstract suite: extract a document with LLMWhisperer, run it
through a Document Studio API deployment, clone one organization's resources
into another. Install it with `pipx`, then `unstract config init`.

## The `unstract` command name

`unstract-client` released before this CLI installed a console script called
`unstract` too, and that script has been removed there — its clone command is
now `python -m unstract.clone`, and this CLI's `unstract clone` wraps the same
code. An environment holding an older `unstract-client` alongside this package
gives the name to whichever was installed last:

```bash
command -v unstract && unstract --version
```

`pipx` avoids the question by giving this CLI its own environment. A second
console script, `unstract-cli`, always belongs to this package.

## Behaviour worth knowing before you script against it

- **A failure the service reports inside a successful HTTP response exits 5
  (validation), not 8 (server error).** Exit 8 invites a retry, and on an API
  that bills per execution a blind retry is a second charge for work that was
  already done. The service's own report is in `error.details`.
- **`clone` exits 0 when nothing failed, which is not the same as everything
  having moved.** Oversize and unsupported documents are skipped by design;
  `data.skipped` counts them.
- **`config doctor` exits non-zero when one of its own checks failed**, so a
  setup script can branch on it. A setting that is simply not configured is
  reported, not failed.
- **A custom `page_separator` needs LLMWhisperer v2.64.2 or later.** An older
  service reads only the previous spelling of the parameter, falls back to the
  default `<<<` separator, and reports no error.

## Consuming the output

Pass `-o json`: stdout is then exactly one `{ok, data, error, meta}` envelope on
success and on failure alike. Ignore fields you do not recognise, refuse a
`meta.contract_version` above the one you were written against, and branch on
the exit code rather than on message text. `unstract --discover full` publishes
the whole contract alongside every command and flag.
