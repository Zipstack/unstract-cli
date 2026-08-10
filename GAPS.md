# Gaps and painpoints

Only things **not** already recorded in the knowledge base. The known ones — the
requests/httpx exception break, the 83-operation annotation backlog, the 9 `operationId`
collisions, the ~6 queryset-introspection failures including `FileHistoryViewSet` hitting
the DB, LLMWhisperer taking query-string params on POST — all held exactly as written and
are not repeated here.

Ordered by how much they should change your confidence in the approach.

---

## 1. The spec's paths were wrong, and it failed as a 401

`02-poc-plan` says "Start with `api_v2.execution_urls` only". A sub-urlconf generates paths
relative to itself, so the spec described `/api/{org}/{api}/` while the server serves
`/deployment/api/{org}/{api}/` (`backend/base_urls.py:20` mounts it under
`settings.API_DEPLOYMENT_PATH_PREFIX`).

The generated client therefore hit a *different* live route, which returned **401, not 404**.
With a valid key in hand, that reads as an auth problem. Fix is three lines in the generator
(mirror the mount), but the failure mode is the concern: a spec can be structurally wrong
about the only thing it exists to describe, and the server will answer plausibly.

## 2. File upload was silently broken, and the throwaway run's evidence did not catch it

`01-current-state` records: *"It handled multipart unprompted — `_kwargs["files"] =
body.to_multipart()`"*. That line is emitted either way. It is not evidence of correct
multipart.

drf-spectacular maps a DRF `FileField` to `{"type": "string", "format": "uri"}` — correct
for *output*, where a FileField serialises to a URL, and wrong for a multipart upload.
`openapi-python-client` faithfully emits `list[str]`, and `to_multipart()` then produces:

```python
files.append(("files", (None, str(files_item_element).encode(), "text/plain")))
```

i.e. it uploads the string representation of whatever you pass, as `text/plain`, with no
filename. No error anywhere. The one thing this endpoint exists to do would have shipped
broken.

Fix, in the annotation, 4 lines:

```python
@extend_schema_field(OpenApiTypes.BINARY)
class UploadField(serializers.FileField): ...
files = serializers.ListField(child=UploadField(), required=False)
```

after which the model is `list[File]` and multipart calls `.to_tuple()`. **The lesson is not
the four lines — it is that "the generator emitted multipart code" was mistaken for "the
generator emitted correct multipart code."** Assume the same class of error in the other 82
unannotated operations.

## 3. `null` where the spec says "optional array" crashes deserialisation

The backend returns `"result": null` on a pending execution. `FileResult(many=True,
required=False)` produces `type: array` without `nullable`, and the generated `from_dict`
does `for x in _result:` with no guard:

```
TypeError: 'NoneType' object is not iterable
```

Every optional collection needs `allow_null=True` declared, and Django REST's habit of
returning `null` rather than omitting the key means this will recur constantly. It fails at
*response parse time* on a live call, so no amount of spec review catches it — only running
the thing does.

## 4. An undeclared status code becomes a wrong error message, not an unknown one

`GET` returns **406** when the result was already acknowledged. That was not in the
annotation, so `_parse_response` returned `None`, and the facade — which cannot tell "body
was not JSON" from "status was not declared" — reported `"Invalid JSON response from API"`.
Confidently wrong.

You must enumerate every status a view can return. For a bare `APIView` with branches
returning 200/406/422/500 there is nothing to introspect, so this is pure manual archaeology
per endpoint, and getting it wrong degrades error reporting silently.

## 5. The compat contract is right about return *shapes* and wrong about return *types*

`03-compat-contract` concludes return values are safe to swap because they are "plain dicts,
assembled key by key". True of the published client. A facade over generated code gets
`attrs` model instances, and putting one in the documented `dict` return blows up at
`json.dumps` — **at the moment a caller serialises, not at import, and only on the code path
where a result actually comes back**.

`to_dict()` fixes it and does round-trip undeclared keys through `additional_properties`, so
nothing the backend sends is lost to an incomplete annotation. Verified: ignoring per-run
IDs and timestamps, the generated and published CLIs produce byte-identical output for both
commands. But this is a per-field discipline in every facade method, and the failure is
runtime-only.

## 6. Two parameters in the published LLMWhisperer client have never reached the service

Found by diffing the AST walk against `client_v2.py`, not by inspection. Neither is in the
KB's drift table.

| Client sends | Service reads | Effect |
|---|---|---|
| `line_spitter_strategy` | `line_splitter_strategy` (`controller_v2.py:563`) | the kwarg is silently discarded; the service always uses its default `left-priority` |
| `filename` | `file_name` (`controller_v2.py:612`) | reports always record `sample.pdf` |

Neither misspelling appears anywhere in the service. `llmwhisperer-client` is ~28,500
installs/month and a hard dependency of `unstract/sdk1` and `unstract-sdk`. Worth a bug
report independent of this POC.

This is also the strongest argument *for* the pipeline in the whole exercise: the drift was
invisible to three code reviews and fell out of a 300-line AST walk immediately.

## 7. The AST walk cannot see parameters read outside the handler

`url` and `url_in_post` — both public features of `whisper()` — are read in
`Util.get_request_data()` (`app/util/base.py:264,279`), not in the route handler. A
single-file walk misses two params on the highest-traffic endpoint in the product.

Handled by pre-scanning the tree and splicing in helpers the handler calls, matched by bare
function name, one level deep. That works today and is a real fragility: two same-named
helpers, or one more level of indirection, and params vanish again — silently, because a
missing param looks exactly like a param that does not exist.

Related, smaller: defaults applied as a post-hoc fallback

```python
x = request.args.get("median_filter_size")
if x is not None: v = int(x)
else: v = DefaultValues.DEFAULT_MEDIAN_FILTER_SIZE
```

are invisible to the walk. Two params needed an overlay entry each for their default.

## 8. `api_timeout` is not a transport timeout, and passing it as one crashes

`APIDeploymentsClient(api_timeout=-1)` means *async execution*, a backend parameter. Feeding
it to `httpx.Timeout` gives `ValueError: Timeout value out of range` deep inside httpcore.
The published client never sets a `requests` timeout at all, so the facade must not either.
Trivial once known; a coincidence of naming that will catch the next person.

## 9. The "free drift signal" is weak for the change it most needs to catch

Measured: renaming one route → **137-line diff in a 436-line spec**, of which 4 lines are
the path keys. Because paths are top-level sorted keys, a rename relocates the entire
operation block. Field additions diff cleanly (adding a serializer field was exactly 4
lines), but renames — the change that actually breaks clients — produce the noisiest diff.
At 307 paths a rename would be unreviewable by eye.

## 10. Annotation cost is ~2× the recorded estimate, and the delta is all real bugs

`01-current-state` records ~30 lines to annotate `DeploymentExecution`. The annotation that
produces a **working** SDK is **62 non-blank non-comment lines plus 10 import lines**, for
one view / two operations — about 31 per operation, sitting exactly on the kill criterion's
"roughly 30 lines on average".

The extra 30 lines are items 2, 3 and 4 above: the binary upload field, the `allow_null`
declarations, the 406/500 responses, and explicit path parameters. None optional; each one
was a live failure. Budget ~30 lines *per operation* for the remaining 83, not ~30 per
endpoint.

## 11. Smaller things that cost time

- **Generation is not static.** `django.setup()` loads every app; ~40s, and a reachable
  Postgres is mandatory. Any CI that gates the spec needs a database.
- **Deriving CLI flags from generated models needs two non-obvious steps**: generated
  models carry *string* annotations (`attrs.resolve_types()` before `attrs.fields()`), and
  `additional_properties` is an attrs field that will become a CLI flag if you do not filter
  it.
- **The Docstudio status GET is one-shot**, like LLMWhisperer retrieve. Reading a completed
  result acknowledges it; the next read is 406. Any comparison harness must give each side
  its own execution, and the CLI must persist before exit.
- **`mcp_server.urls` rides along** inside `api_v2.execution_urls`, so the deployment spec
  gets two extra `mcp` operations that introspect to nothing. Harmless, but it means
  "generate just the routes I want" is not actually available.

---

## Verdict

**The pipeline works and is worth continuing.** Every acceptance criterion in
`02-poc-plan` was met — deterministic regeneration, no post-processing of generated code, a
backend field reaching a CLI flag with zero hand-written lines, and a generated-SDK CLI that
is output-identical to the published one. No kill criterion was hit.

The honest caveat is that **every single defect above was silent**. Nothing failed at
import. Nothing failed in a test that did not make a live call. Wrong paths returned 401,
wrong file encoding returned 200, an undeclared status produced a confidently wrong error
message, and a leaked model type only exploded when someone serialised the result. The
generator's output is plausible far more often than it is correct.

That has one concrete consequence for what comes next: **an annotation is not done when the
spec generates, it is done when a live round trip passes.** Whatever this becomes, the
per-endpoint cost includes an integration test, not just `@extend_schema` lines. If that is
priced in, the approach beats hand-transcribing 148 endpoint records — item 6 is the proof,
and it was free. If it is not priced in, this generates wrong clients faster than anyone can
review them.
