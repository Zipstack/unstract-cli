# OpenAPI schema — options and recommendation

Status: **proposal, not adopted.** Written up from a review discussion on
[#1](https://github.com/Zipstack/unstract-cli/pull/1); nothing here is implemented.

The question that prompted it: *where should the OpenAPI schema live — this repo or
somewhere else?* Answering that needs the surrounding decisions too (whether to
generate one at all, with what tooling, and what consumes it), so those are here as
well.

---

## 1. The problem this addresses

This CLI hand-encodes **148 endpoint records and 615 parameters** against an API with
no published machine-readable schema:

| API group | records |
|---|---:|
| `platform` | 125 |
| `llmwhisperer` | 11 |
| `apihub` | 6 |
| `deployment` | 3 |
| `hitl` | 3 |

Every record was transcribed by hand from prose documentation across 27 distinct
`doc_source` files. The only automated check that a record still matches reality is
`tests/test_skill_docdiff.py` (13 tests), which diffs records against those same prose
docs — so it verifies the CLI against *documentation*, never against the *code*.

That gap is not theoretical. The pre-merge review of #1 produced five findings that
were record-vs-backend drift rather than logic errors:

| Finding | Drift |
|---|---|
| `workflow execute` | record said `BodyKind.JSON`; the endpoint takes multipart, so the JSON body was silently dropped |
| `--shared-users` / `--shared-to-org` | declared on 19 records; backend removed the M2M and marked the field read-only |
| group-member `DELETE` | record declared no trailing slash; the router requires one |
| `deployment run --wait` | record expected `execution_id` in the body; it exists only in a query string |
| `--include-metrics` | supported by the backend, absent from the records entirely |

A generated client cannot have any of these.

Worse, the drift check itself proved unreliable in the direction that matters. After
fixing the sharing flags against the backend, the drift suite went **red** — because
`unstract-docs` still documents the removed fields. Docs last updated 2026-07-08; the
`absorb_shared_users` migration landed 2026-07-17. **The documentation was the stale
side, and the drift suite was arguing for the broken behaviour.**

---

## 2. Current state of the backend (verified)

Checked against `unstract@023b14021` (OSS) and `unstract-cloud@78e783e5`.

- **`drf-yasg>=1.21.8` is already a declared dependency** — `backend/pyproject.toml:30`,
  and `"drf_yasg"` is in `INSTALLED_APPS` (`settings/base.py:358`).
- **A schema view already exists** — `backend/docs/urls.py` builds one via
  `get_schema_view(...)`.
- **But it serves no machine-readable document.** Only `schema_view.with_ui("redoc")`
  is mounted. There is no `without_ui(format="json")` route, so there is nothing to
  fetch, diff, or generate from.
- **And it is mounted in the wrong urlconf.** `docs.urls` is included from
  `backend/public_urls_v2.py`, the *public* schema urlconf. The tenant-scoped routes —
  `/api/v1/unstract/{org_id}/...`, i.e. the 125 records this CLI encodes — live in
  `urls_v2.py` and are **not covered**.

So the groundwork is roughly half-done, and the half that is done is pointed at the
wrong routes.

> Note: `/api/v1/unstract/{org_id}/...` is not a versioned public contract. `v1` is the
> `PATH_PREFIX` env string, and it is the same urlconf the React frontend calls. There
> is no deprecation policy, so any normal feature PR can move a route.

---

## 3. Where the schema should live

**Recommendation: generate and commit it in the backend repo that owns the routes;
consume it here. Do not vendor a copy in this repo.**

| Repo | Holds | Why |
|---|---|---|
| `unstract` (OSS) | `openapi.json`, generated + committed, CI-gated | Generated from the routes and serializers it sits beside, so it cannot drift from them |
| `unstract-cloud` (Enterprise) | its own schema, if it adds routes | The OSS schema will not describe Enterprise-only endpoints |
| `unstract-cli` (here) | **consumes** it; no committed copy | A checked-in copy becomes a third artifact to keep in sync |

Three reasons, in order of weight:

1. **Co-location with the source of truth.** The schema is derived from routes and
   serializers. Committed anywhere else, it can lag the code it describes — which is
   precisely the failure mode already demonstrated by `unstract-docs`.
2. **The gate has to run where the change happens.** The value is a *backend* PR
   failing when it renames a route. A check in this repo learns about it afterwards,
   which is the situation today.
3. **Enterprise needs its own.** A single schema in either repo would be wrong for the
   other; the split mirrors how the code is already split.

**Consumption from this repo** should follow the pattern the drift job now uses for the
docs repos: check the backend out alongside in CI, or fetch the committed artifact. Not
a vendored copy.

---

## 4. Options considered

### Option A — `drf-spectacular`, mounted in the tenant urlconf ✅ recommended

Add `drf-spectacular`, mount `SpectacularAPIView` **inside** `urls_v2.py`, commit the
generated `openapi.json`, and add a CI step failing when the committed file is stale.

- **For:** OpenAPI 3.x, which is what every CLI/SDK generator expects. Actively
  maintained. Covers the tenant routes the CLI actually uses.
- **Against:** a new dependency; `drf-yasg` then has two schema stacks or needs
  removing. Serializer annotations needed where introspection is ambiguous.
- **Cost:** days, not hours — mostly annotating endpoints whose request shape
  introspection cannot infer.

### Option B — mount `drf-yasg`'s JSON route in the tenant urlconf

Add `schema_view.without_ui(format="json")` to the tenant urlconf, reusing what is
already installed.

- **For:** no new dependency; smallest diff.
- **Against:** `drf-yasg` emits **Swagger 2.0**, not OpenAPI 3. Generators would need a
  conversion step, and it is the less actively maintained option. Locks in a format
  that has to be migrated later anyway.
- **Verdict:** viable as a stopgap; not what to build on.

### Option C — resolver-diff CI job (no schema) ✅ recommended interim

A backend CI job that diffs this CLI's `ALL_ENDPOINTS` against
`django.urls.get_resolver()`.

- **For:** ~30 lines, no new dependency, no annotation work. Catches renamed and removed
  routes immediately — which is the majority of the drift found in the review.
- **Against:** paths only. Cannot see request/response *shapes*, so it would have caught
  the trailing-slash finding but **not** the `workflow execute` body-kind or the
  read-only `shared_to_org`. No generation.
- **Verdict:** the right thing to do first, and it stays useful even after a schema
  lands.

### Option D — status quo (prose docs + `docdiff`)

- **Against:** already demonstrated to fail. Docs lag the code by unbounded time, and
  the drift suite inherits that lag — arguing for removed fields, as it did here. Five
  drift findings in one review.

### Option E — generate the CLI itself from the schema

Once a schema exists, replace the hand-written registry with generated commands (Fern,
Speakeasy, Stainless, or `openapi.json` → records).

- **For:** removes hand-maintenance entirely; the 148 records stop being an artifact to
  keep in sync.
- **Against:** only possible *after* Option A. This CLI's agent ergonomics — `--discover`,
  stable exit codes, JSON error envelopes, one-shot `--save` semantics — are ahead of
  what stock generators emit, so generation must not regress them.
- **Verdict:** the eventual destination, explicitly out of scope here.

---

## 5. Suggested sequencing

1. **Option C now.** Resolver-diff job in the backend. Cheap, immediate, closes the
   largest class of drift.
2. **Fix the docs.** `unstract-docs` still documents `shared_users` / `shared_to_org` on
   the per-resource endpoints. Until corrected, anyone reading them will believe those
   fields work. This CLI carries a temporary exemption in `src/unstract_cli/skill/docdiff.py`
   (`_REMOVED_UPSTREAM`) — **delete it once the docs are updated.**
3. **Option A when there is appetite.** `drf-spectacular` in the tenant urlconf,
   committed artifact, CI gate.
4. **Option E only after A has been stable for a while.**

---

## 6. Open questions

- **Who owns regeneration?** A committed artifact needs someone responsible when it goes
  stale, or it becomes another `unstract-docs`.
- **Does Enterprise need a merged view?** A CLI serving both would need two schemas, or
  a documented merge.
- **How are non-schema behaviours captured?** Several records encode facts no schema
  expresses: one-shot reads that a retry destroys, the 422-for-in-progress quirk, routes
  where `PATCH` semantics differ from `PUT`. These stay hand-maintained regardless, and
  a generated registry must not silently drop them.
- **Versioning.** `v1` is currently an env-var string with no deprecation policy. A
  published schema invites treating it as a real contract — worth deciding deliberately
  rather than by accident.

---

## Appendix — evidence

| Claim | Source |
|---|---|
| `drf-yasg` declared but serves no JSON | `backend/pyproject.toml:30`; `backend/docs/urls.py` (only `with_ui("redoc")`) |
| Schema view mounted in the public urlconf | `backend/backend/public_urls_v2.py:29` |
| Tenant routes not covered | `backend/backend/urls_v2.py`; `ROOT_URLCONF = "backend.base_urls"` (`settings/base.py:422`) |
| `shared_users` removed from six models | `*/migrations/*absorb_shared_users.py` (2026-07-17) |
| `shared_to_org` read-only in six serializers | `adapter_processor_v2`, `api_v2`, `connector_v2`, `pipeline_v2/serializers/crud`, `prompt_studio_core_v2`, `workflow_v2` |
| Sharing honoured only via `platform share` | `backend/permissions/resource_share_views.py:20` (`_SUPPORTED_SHARE_AXES`) |
| Group-member DELETE requires a slash | `DefaultRouter(trailing_slash=True)` in `tenant_account_v2/groups_urls.py`; resolves to `members/(?P<user_id>[^/.]+)/` |
| Docs stale vs backend | `unstract-docs` latest 2026-07-08; migration 2026-07-17 |
