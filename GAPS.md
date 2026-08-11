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

> **Fixed upstream 2026-08-11.** `llm-whisperer-python-client` `e8935b3` — *"send query params
> under the names the service reads"* — adopts this finding and answers LW-406. `page_separator`
> and `file_name` **forward** from their deprecated spellings; `line_splitter_strategy` stays
> **dead**, because the value never reached the service and applying it now would silently change
> extraction output. The facade mirrors the resolver, including which of the three forwards.

| Client sends | Service reads | Effect |
|---|---|---|
| `line_spitter_strategy` | `line_splitter_strategy` (`controller_v2.py:563`) | the kwarg is silently discarded; the service always uses its default `left-priority` |
| `filename` | `file_name` (`controller_v2.py:612`) | reports always record `sample.pdf` |

Neither misspelling appears anywhere in the service. `llmwhisperer-client` is ~28,500
installs/month and a hard dependency of `unstract/sdk1` and `unstract-sdk`. Worth a bug
report independent of this POC.

This is also the strongest argument *for* the pipeline in the whole exercise: the drift was
invisible to three code reviews and fell out of a 300-line AST walk immediately.

Confirmed against the tip of `origin/main` in both repos — client `3832713`
(`client_v2.py:460`, `:466`) and service `237870ca` (`controller_v2.py:571`, `:621`) — so it
is not an artefact of a stale checkout. Raised as **LW-406**.

Three things the follow-up surfaced that are not obvious from the table:

- **Impact is uneven.** `filename` affects *every* caller, because the service default is
  `"sample.pdf"` and so every usage report row is wrong. `line_spitter_strategy` affects only
  callers who explicitly set `right-priority`/`mid-priority`; client and service defaults are
  both `left-priority`, so everyone else is unaffected.
- **Fixing it is a behaviour change, not a bug fix, from the caller's side.** A parameter
  that has always been a no-op starts taking effect on upgrade. Anyone who tuned around it
  doing nothing gets different extraction output.
- **The service validates the strategy** and raises `BadRequest` for an unknown value
  (`controller_v2.py:578`). Today a typo is silently swallowed; once the param arrives, the
  same typo becomes a hard 400.

Why three reviews missed it: the service's *local variable* is also spelled
`line_spitter_strategy`. Only the query key it reads is correct, so anyone comparing variable
names sees a match.

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

## 11. A request body the AST walk cannot see generates a model with no fields

The webhook endpoint takes its payload as JSON in the body (`json.loads(request.get_data())`
in the shared `whisper_callback` handler). The walk only reads `request.args`, so the overlay
had to supply the body — and it supplied it as `{type: object, additionalProperties: true}`.

That generates a `WebhookPostBody` with **zero attrs fields**. Nothing in the pipeline
complains. `register_webhook(url=…, auth_token=…, webhook_name=…)` cannot be expressed, and
had it shipped, every call would have returned `400 "Webhook name not provided in JSON"` —
which reads as a caller bug, not a client bug.

Fixed with a named `WebhookConfig` schema, 7 overlay lines. The lesson is the same as items 2
and 4: **an untyped placeholder in the overlay is indistinguishable from a correct one until
something calls it.** Any `additionalProperties: true` in the overlay is an unpaid debt.

Related, cosmetic: `whisper_callback` serves POST/GET/PUT/DELETE from one function, and the
walk attributes every branch's params to every method — so `webhook_post` carries a
`webhook_name` query param that only the GET/DELETE branches read. The server ignores it.

## 12. One of the eleven LLMWhisperer methods makes no HTTP call at all

`get_highlight_rect()` is pure geometry over `line_metadata` — no request, no endpoint,
nothing to generate. It has to be copied into the facade verbatim.

Small in itself, but it breaks the mental model: "generate the client" is never literally all
of the client, even for methods that look like API calls from the outside. Any per-endpoint
cost estimate should assume a tail of methods that are not endpoints.

## 13. The facade is looser than the client it replaces, by accident

`check_execution_status()` in the published client expects the **relative** endpoint the API
returns, and prefixes its own `base_url`. Hand it an absolute URL and you get
`https://host…https://host…`. The generated facade accepts both.

Accepting a superset breaks no caller, so this is not a compat failure. It is a warning about
method: a facade rebuilt from a spec will differ in *input handling* wherever the published
client's behaviour came from an implementation detail rather than the contract, and nothing
in the pipeline surfaces that. `test_llmw_compat.py`-style signature checks do not catch it —
signatures matched perfectly here. Only a differential test on inputs would.

## 14. The generated client sends every spec default, and that changes the answer

`_get_kwargs` writes each parameter's declared `default` into the request. The published
client sends only the parameters it knows about. So the generated side puts **seven extra
query parameters** on `whisper` (`allow_rotated_text`, `checkbox_confidence_threshold`,
`derotate_threshold`, `ignore_vertical_text`, `min_table_width`,
`watermark_angle_threshold`, `word_confidence_threshold`), one on `retrieve` (`text_only`),
and **four extra multipart fields** on deployment execute (`include_extracted_text`,
`include_metrics`, `tags`, `use_file_history`).

Sending a default is not the same as omitting it. It pins a value the server would otherwise
choose for itself, and the two disagree the moment the service changes a default.

Measured on staging, same PDF, both CLIs, deterministic on each side:

| | published | generated |
|---|---|---|
| Greek | `Γαζέες και μυρτιές` | `Γαζέες␣␣␣␣μυρτιές` |
| Thai | `· เป็น มนุษย์ …` | `␣␣เป็น มนุษย์ …` |

Every word scoring 0.053–0.279 vanished from `result_text` *and* from `confidence_metadata`.
Bisected one parameter at a time against the published baseline: `word_confidence_threshold`
alone reproduces it, the other eleven are innocent. Note the checkout of the service reads
`request.args.get("word_confidence_threshold", 0.3)` — the same value the spec declares — so
staging is running a build whose default differs. That is the whole point: **a spec default
is a snapshot of the server's default at generation time.**

**Revised 2026-08-11.** The published client used as the baseline above was 2.5 months
stale. At `3832713` it grew `word_confidence_threshold` (PR #33) and now sends `0.3`
unconditionally, documented as excluding any word below the threshold from the output. So
for *this* parameter the two clients were at different versions, not disagreeing about
defaults, and the dropped words are intended product behaviour rather than a client bug.

What the bisect proved is unaffected: the service returns different text for *absent* than
for *0.3*, so sending a declared default is not the same as omitting it. The other six
`whisper` parameters, `retrieve`'s `text_only`, and the four multipart fields on deployment
execute remain genuine over-sends that no published client emits. See §17 for the version
skew itself.

Two fixes work, and the choice is not obvious:

- **Spec side.** Stripping the 50 `default:` keys before generating makes the generator emit
  `UNSET` and omit unset parameters entirely — verified, and the 28-parameter signature
  survives, so CLI flag derivation is unaffected. Fixes every parameter and every backend at
  once. Costs the default value in `--help` and in the signature, which is real documentation.
- **Facade side (taken).** `_call(..., send_only=…)` keeps only the parameters the caller
  actually set; the deployment facade resets untouched `ExecuteRequest` fields to `UNSET`.
  Exact — a caller who explicitly passes the default value still sends it — but it is
  per-method, so a method added later reintroduces the bug silently.

`test_llmw_compat.py::check_no_injected_defaults` and `test_compat.py` guard both backends
either way, and both were confirmed to fail with the fix removed.

## 15. The facade had drifted where no test was looking: the constructor and the retry policy

Found by the same input diffing, not by the signature check that was supposed to cover it.

`check_surface` iterates public methods, and `__init__` starts with an underscore:

```
pub: (base_url, api_key, logging_level, custom_headers, max_retries, retry_min_wait, retry_max_wait)
gen: (base_url, api_key, api_timeout, logging_level)
```

Third positional was `api_timeout` where published has `logging_level`, so
`LLMWhispererClientV2(url, key, "INFO")` sets a timeout to a string. Four keyword arguments
were missing outright. `api_timeout` is not a constructor argument in the published client at
all — it is a class attribute (`client_v2.py:95`). The facade's default `base_url` was also
missing `/api/v2`, so `client.base_url` read back wrong.

The retry policy had diverged just as far: three fixed attempts against a hardcoded status
set, `2**n` backoff, and **transport errors not retried at all**, against published's
`max_retries+1` attempts, `429 or >= 500`, `ConnectionError`/`Timeout` retried, and
exponential jitter bounded by two constructor arguments the facade did not have.

None of this is generatable and none of it is spec drift — it is facade drift, which is the
category the whole design leans on staying correct. `check_construction` now covers the
signature and the defaulted attribute values.

## 16. Smaller things that cost time

- **Generation is not static.** `django.setup()` loads every app; ~40s. It does **not** need
  a database, though — measured 2026-08-11 against a dead port, the spec is byte-identical
  for both the deployment and the full tenant urlconf. The startup DB error is caught and
  logged, and the two viewsets whose `get_queryset()` drf-spectacular calls fail with a live
  DB too. CI needs the backend venv and nothing else.
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

## 17. The baseline moved under the test, and the test was measuring a working tree

Both published clients are installed `-e` from local checkouts, so every compat assertion is
made against whatever happens to be checked out — not against a released version. On
2026-08-11 `llm-whisperer-python-client` was pulled from `9862b8f` (2026-05-23) to `3832713`,
and `test_llmw_compat.py` went from green to:

```
AssertionError: whisper lost word_confidence_threshold
```

Nothing in this repo had changed. Two separate problems sit behind that one line:

- **The comparison has no fixed baseline.** A CI run must pin the published client to a
  released version, or the drift check reports on a colleague's feature branch. Same class of
  hazard as the unpinned generator (`gen_sdk.sh` now pins `openapi-python-client==0.29.0`);
  this one is still open.
- **It invalidated a conclusion after the fact.** §14's headline example was measured against
  a baseline that has since moved, and the "the generated side is wrong" framing did not
  survive. The mechanism did. Any measurement taken against an unpinned dependency has a
  shelf life, and this one lasted a day.

The upside is that the failure mode worked exactly as designed: an offline, sub-second check
named the exact missing parameter, the facade fix was one line in two places, and both CLIs
picked the new flag up with no edit at all. That asymmetry — **generated transport and CLI
auto-expose, the facade never does** — is the cost of pinning the published contract, and it
is the one place a human is structurally required.

## 18. The facade read the generated model, and the generated model is not always there

Found by writing `poc/test_error_parity.py`, which points both clients at a local server that
returns a scripted status and body. Two defects on the first run, both in code that had passed
every existing check and a live round trip:

- **A 500 with a well-formed body crashed.** `_unwrap_execute` read `resp.parsed.message`, but
  the generator types an error body's `message` as a bare object, so `.execution_status` raised
  `AttributeError` where the published client returned a shaped error.
- **A 401 on the status endpoint reported the wrong thing.** 401 is not a declared status for
  that operation, so `resp.parsed` was `None` and the facade returned `"Invalid JSON response
  from API"` for a response that was perfectly valid JSON.

Both have one root cause: the facade trusted `resp.parsed`, which exists only for statuses the
spec declares and is typed loosely for error bodies. The published client reads the JSON body
directly. The facade now does the same, which also reproduces the published client's own
`AttributeError` when `message` arrives as a string — a drop-in replacement inherits the
contract including its bugs.

Two more things fell out of the same change:

- **`_parse_response` calls `.json()` unguarded for every declared status**, so an empty or
  truncated body escaped as `JSONDecodeError` where the published client returned a shaped
  error. Since the facade no longer needs `resp.parsed`, it now issues the generated request via
  `_get_kwargs` and skips the generated response parsing entirely. This removes the whole class,
  including gap 3.
- **The status endpoint was over-sending two injected defaults.** `status._get_kwargs` writes
  `include_extracted_text` and `include_metrics` into every request; the published client sends
  only `include_metadata`. This is gap 14 on the deployment side, which no check had covered
  because the compat test inspects the execute *body* and not the status *query*. Fixed with the
  same `send_only` filter the LLMWhisperer facade already used.

The measurement that matters: **25 comparisons, 5 methods, 5 bodies each.** Everything the
published client does on an error path is now either matched or listed with a reason. The
remaining 13 accepted divergences are all one finding — the published client handles errors two
different ways depending on which method you call (`whisper_status` and `whisper_detail` guard
the parse and set `.status_code`; `get_usage_info`, `whisper_retrieve`, `get_highlight_data` and
the webhook methods do neither), and the facade applies the guarded form everywhere.

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

### Update after the staging run

The live gate has now been cleared for both backends against `globe.unstract.com`: Document
Studio execute and status are output-identical to the published client on both a success and
a 422 path, and upload fidelity is confirmed in the database. `MEASUREMENTS.md` has the
numbers.

Three of the four findings added at that point — items 11, 12 and 13 — are the same shape as
the original five: a placeholder that generates cleanly and fails only when called, a method
that looks generatable and is not, and a behavioural difference that signature checks pass
right over. Treat "it generated" as carrying no information whatsoever.

### Update after the input diff

Item 13 named the remaining hole — differential behaviour on *inputs* — and closing it was
worth the cost. Adding `whisper` and `webhook` commands to `cli_published.py` and comparing
the two query strings found **three more defects in one pass**: the injected spec defaults
(item 14) and both halves of item 15.

The first of those was not a latent risk. It was already wrong on staging, in the one output
the client exists to produce, and the earlier LLMWhisperer verification recorded here and in
`MEASUREMENTS.md` as "returned correct text" was reading text with words missing. Output
comparison could not have caught it, because there was nothing to compare against — the
published client had no CLI wrapper, which is exactly why that gap was listed.

Twelve silent defects out of twelve. The pattern is now specific enough to act on: **compare
what the two clients send, not only what they return.** Signature equality is necessary and
nowhere near sufficient, and the parts of a client no spec describes — constructors, retry
policy, which parameters get omitted — are where a facade drifts, because nothing regenerates
them and nothing checks them.
