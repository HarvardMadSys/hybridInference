# User tags and the `unattended` tag

A **user tag** names a group of accounts and holds them to limits of its own:
a daily and a monthly cost quota, a model allowlist and a cap on concurrent
requests. An administrator adds accounts to a tag by hand. A tag can also have
an **automatic rule**, which adds every account that sent requests in at least
*K* of the last *N* hours.

The gateway ships one tag with a rule: **`unattended`**. A person doesn't send
requests every hour of a twelve-hour stretch; a script, a cron job or an agent
left running does. The tag starts with no limits, so it only labels accounts
until an administrator gives it some.

Tags are managed in the admin console under **Users → User tags**, or through
the admin API below.

## What a tag's limits mean

Each limit on a tag is a **ceiling**. A member is held to the strictest of its
own limit and the limits of every tag it carries:

| Limit | Combines as |
|---|---|
| Daily cost quota | The lowest of the account's own and each tag's. An account with no daily quota of its own counts as the 1000 USD default. |
| Monthly cost quota | The lowest of the account's own (none means no limit) and each tag's. |
| Concurrent requests | The lowest of the account's cap (its own override, else its role's) and each tag's. The tag cap also limits an unlimited role, and applies to the concurrency-exempt and token-counting budgets too. |
| Models | The member may use only models on **every** tag's allowlist, on top of the role, disabled-model, key and project checks. A tag without an allowlist doesn't restrict models. Allowlists that share no model allow none. |

So a tag can only narrow what an account may do. An automatic tag can't loosen
a limit an administrator set on the account, and two tags never conflict over
which one wins. To give a group *more* than its accounts have, raise the
accounts' own limits.

The limits apply on every way in: an API key, an inference grant from the cloud
agent, a request made on a user's behalf (`X-On-Behalf-Of`) and the console
playground. Batch work is held to a tag's quotas and allowlist; its concurrency
is the batch scheduler's own per-role limit, which ignores per-account caps too.
The model listings (`/v1/models`) and the console's model pickers leave out
models a tag doesn't allow. `GET /v1/key` and the usage dashboard report the
concurrency cap the tags impose.

A change to a tag's limits applies to its members on their next request in the
process that made it, and in other gateway processes within 30 seconds (their
cached auth lookups expire).

Amounts are in the ledger currency ([Accounting](accounting.md)). A write that
sets an amount names the currency it was read in, and is refused (409) if the
ledger's currency has changed since.

## The automatic rule

A tag's rule has a window of *N* hours (1–168) and a threshold of *K* active
hours (1–*N*). Every ten minutes, a scheduled job counts, for each account, the
distinct hours among the last *N* in which it sent at least one request. It
counts rolling hours back from now, not clock hours. The job gives the tag to
every active account with at least *K* such hours.

- A request is a row of `api_logs` attributed to the account. Rows written by a
  batch job are left out: the gateway sends batch requests at its own pace,
  whoever is watching.
- `unattended` defaults to *every one of the last 12 hours* (12 of 12). Use
  *Preview matches* in the editor to see who a different rule would match before
  saving it. For example, *20 of 24* catches fewer heavy human users, and
  *1 of 6* tags anyone active in the last six hours.
- One run happens at a time across all replicas: the job holds a PostgreSQL
  advisory lock, and a replica that can't take it skips its turn. **Run rule
  now** in the console runs a tag's rule immediately.

### An automatic tag stays until an administrator removes it

The rule only adds members. If it also removed them, a quota or allowlist on
the tag would undo itself: requests refused by the tag's own limits aren't
logged, so an account the tag stopped would soon stop matching, lose the tag
and its limits, and start again.

When you remove a tag that has a rule, the membership is kept as **dismissed**.
A dismissed membership applies no limits. The rule gives the tag back only if
the account's requests *since the dismissal* match it again, so a false positive
stays off unless the behaviour starts again. Removing the dismissed marker as
well (the × on a struck-through tag) forgets the dismissal. Adding the tag by
hand turns any membership into a manual one.

## Admin API

All endpoints require admin authentication (a console session or
`ADMIN_TOKEN`). Every write is recorded in the audit log under a `user_tag.*`
action.

| Method and path | What it does |
|---|---|
| `GET /admin/user-tags` | Every tag with its limits, rule and member counts, built-in tags first. |
| `POST /admin/user-tags` | Create a tag: `name` (lowercase letters, digits, `-`, `_`; at most 32), `description`, the limits and the rule. |
| `PATCH /admin/user-tags/{name}` | Change only the fields sent. `null` clears a limit. |
| `DELETE /admin/user-tags/{name}` | Delete a tag and its memberships. The built-in tag can't be deleted (409); turn its rule off instead. |
| `GET /admin/user-tags/{name}/members` | The tag's memberships, applied ones first; `source` (repeatable) filters by `manual`, `auto` or `dismissed`. |
| `POST /admin/user-tags/{name}/members` | Add accounts by hand: `{"user_ids": [...]}`. Unknown ids are reported, not refused. |
| `DELETE /admin/user-tags/{name}/members/{user_id}` | Remove the tag from an account; under a rule, the membership is kept as dismissed. |
| `GET /admin/user-tags/{name}/preview` | The accounts a rule matches now, read only. Optional `window_hours` / `min_active_hours` try bounds that aren't saved yet. |
| `POST /admin/user-tags/{name}/detect` | Run a tag's rule now (409 when the rule is off). |

The tag fields are `quota_daily_cost`, `quota_monthly_cost` (with `currency`),
`max_concurrent_requests`, `allowed_models` (canonical model ids; an alias is
stored as its model), `auto_enabled`, `auto_window_hours` and
`auto_min_active_hours`.

`GET /admin/users` accepts `tag=<name>` to list only the users a tag applies
to, and each user carries `tags`. `GET /admin/users/{id}` returns the account's
`tag_memberships` (dismissed ones included) and `tag_limits`, the combined
limits of its applied tags.

## Storage

Migration `0023_user_tags` adds two tables and seeds `unattended`:

- `user_tags`: one row per tag with its limits (`NULL` sets none) and rule;
  `system` marks a built-in tag.
- `user_tag_members`: `(user_id, tag)` with `source` (`manual`, `auto` or
  `dismissed`), `assigned_at` and `assigned_by` (the administrator's user id;
  `NULL` for the rule). A membership is deleted with its account or its tag.

The auth lookups join an account's applied tags in the same query and fold
their limits into the row ([`serving.user_tags`](https://github.com/HarvardMadSys/hybridInference/blob/dev/apps/backend/serving/user_tags.py)).
For an account without tags this costs one primary-key probe.
