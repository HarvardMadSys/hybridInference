# Handoff: runtime-route `include_usage_in_stream` inheritance gap

Date: 2026-10-23
For: the maintainers/agent owning #1630 (edit route capabilities at runtime),
#1631 (edit/disable/delete/restore routes), #1632 (static provider-key lookup
on route edit). Written by the routing-side cache-locality work; none of your
branches are edited here.

Status: the create, restore, and kind-repoint inheritance gap described below
was fixed by PR #1635 (`_config_to_dict(..., strip_route_capabilities=True)`);
same-endpoint edits retain the existing route value. Tests now cover create,
same-endpoint edit, repoint, and restore. This note records the original
finding and its resolution.

## Original classification (as of 2026-10-23; superseded below)

The mechanism is **not** introduced by PR A
(`bamn/serving/route-usage-capability`, `fac7ffc0` equivalent): the template
config copy (`_config_to_dict` walks every `ModelConfig` field via
`asdict`), the presence-based kind default in `_make_adapter`
(`"include_usage_in_stream" not in cfg`), the kind-repointing edit
(`UpdateProviderRouteRequest.upstream_provider`), and restore-from-row
(`_stored_capabilities`, exactly four declared fields) all predate it. The
codebase even already strips one such uncloneable per-endpoint fact
(`_UNCLONEABLE_ENDPOINT_FACTS = ("null_cache_details_means_miss",)`),
showing the class of bug is known. What PR A adds is a new per-route
transport field that the pre-existing mechanism now applies to — so the gap
becomes observable for usage accounting, but no A code path regresses. PR A's
YAML-declared semantics (explicit value wins, kind default only on absence)
are correct on every path that re-reads models.yaml.

This is an **endpoint transport capability** (does this upstream answer
`stream_options.include_usage` with a final usage chunk?), not a model
context/tool capability; `RuntimeModelCapabilities` is the wrong schema home
for it semantically, though the persistence fix may naturally extend that
row's columns.

## Exact reproduction (three scenarios)

Setup: one model, first route is a `vllm`/`deepseek`/`sglang`/`zai`/`kimi`/
`minimax` kind (in `_STREAM_USAGE_DEFAULT_KINDS`, so the resolved flag is
True), and a second runtime route on a kind whose default is False (e.g.
`ollama`, or any kind outside the set that rejects unknown body fields).

1. **Create (inheritance).** `POST /routing/provider-routes/...` adding a
   route of the second kind. `_prepare_route_candidate` builds
   `cfg = _config_to_dict(template_adapter.config)`; the copied dict carries
   `include_usage_in_stream: True`. `_make_adapter(second_kind, cfg)` sees the
   key present and skips the kind default. The new route now sends
   `stream_options.include_usage` to an upstream whose kind default says it
   should not — the exact request the default exists to avoid — and an
   upstream that 400s on unknown body fields fails every stream on that route.

2. **Edit (kind repoint).** `PUT .../provider-routes/{id}` with
   `upstream_provider` changed to the second kind. Same copy through
   `_config_to_dict(current_adapter.config)`: the old kind's resolved value
   rides onto the new kind; the new kind's default never applies. There is no
   request field to redeclare or clear it, so the state is unrecoverable
   through the API short of deleting the route.

3. **Restart/restore (rederivation).** The candidate row persists only the
   four declared capabilities (context length, output length, modalities,
   tools); the usage flag is not persisted. On restore,
   `_prepare_route_candidate(restoring=True)` rebuilds cfg from the template
   again — so the restored value is whatever the *template* resolves to, and
   an explicit per-route declaration (had one existed) is indistinguishable
   from an inherited kind default. Value at create, value after edit, and
   value after restore can therefore disagree without any operator-visible
   statement having changed.

Root cause in one sentence: the runtime paths copy *resolved* config values
where the loader's contract is *declared* values win, kind defaults only on
absence — and nothing records which one a route is carrying.

## Minimal design proposed at the time

Keep the flag kind-derived by default and persist only explicit declarations:

- **Storage**: extend the runtime-candidate row with an explicit-override
  column for this flag (or generalize the `_DECLARED_CAPABILITIES` marker
  pattern already used for the four fields) — nullable, NULL = "not declared,
  derive from kind".
- **Create**: do not copy the flag through the template dict; leave it absent
  so `_make_adapter` applies the new route's own kind default. This is a
  one-line change to the runtime create path (strip the key after
  `_config_to_dict`) and fixes scenario 1 outright.
- **Edit**: on a kind repoint, strip the inherited (undeclared) value so the
  new kind's default applies; keep a declared value. Fixes scenario 2.
- **Restore**: declared value wins; absent → kind default re-derived for the
  route's kind. Fixes scenario 3.

This matches the existing loader contract ("Presence, not truthiness"), needs
no new admin API surface to be correct, and remains forward-compatible with
exposing the flag in the console later. All of it lives in
`provider_routes.py` / `schemas_admin.py` / storage — the files #1630-#1632
are actively reshaping — hence this handoff rather than a competing patch.

## Resolution in PR #1635

The create and restore paths strip the template's resolved value before the
target adapter is built, so its own kind default applies. An edit strips the
value only when provider or URL changes; an edit to the same endpoint retains
the route's current value. The setting remains explicit in YAML for routes
that declare it. OpenRouter's adapter always requests usage, and the native
Anthropic, Claude, and Gemini adapters do not consume this flag; the developer
guide now documents those exceptions.
