# unstract-cli — `poc/openapi-pipeline`

A proof-of-concept for keeping an SDK and CLI in step with two backends without
hand-maintaining an API contract:

```
backend routes/serializers  ──drf-spectacular──▶  specs/docstudio.json    ─┐
Flask route handlers        ──AST walk────────▶  specs/llmwhisperer.json ─┴─▶ openapi-python-client
                                                                              │
                                                        build/sdk_*/  (generated, gitignored)
                                                                              │
                                                        poc/facade.py (hand-written)
                                                                              │
                                                        poc/cli_generated.py
```

**Start here:**

| Read | For |
|---|---|
| [`RUNBOOK.md`](RUNBOOK.md) | Exact commands to regenerate and run everything, plus the environment gotchas. Written to stand alone. |
| [`MEASUREMENTS.md`](MEASUREMENTS.md) | The Phase 6 numbers and the kill-criteria verdict. |
| [`GAPS.md`](GAPS.md) | What broke, what it costs, and what to be careful about. |

Nothing in this branch touches any backend repo. The one annotation the POC needs is applied
as a runtime monkeypatch (`tools/annotations.py`), written so it can be pasted into
`api_v2/api_deployment_views.py` unchanged.
