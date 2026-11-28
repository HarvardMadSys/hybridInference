# User tags and the `unattended` tag

A user tag groups accounts and holds them to its own limits: a daily and a monthly cost quota, a model allowlist and a cap on concurrent requests. Administrators add accounts by hand. A tag with an automatic rule also gains every account that sent requests in at least *K* of the last *N* hours. Manage tags under **Users → User tags** in the admin console, or through the [admin API](#admin-api).

The gateway ships one tag with a rule, `unattended`. A person doesn't send requests every hour for twelve hours; a script, a cron job or a forgotten agent does. The tag has no limits until an administrator sets some, so at first it only labels accounts.

## How limits combine

A tag's limits are ceilings. A member is held to the strictest of its own limit and the limits of every tag it carries:

| Limit | Combines as |
|---|---|
| Daily cost quota | The lowest of the account's own and each tag's. An account without a daily quota of its own counts as the 1000 USD default. |
| Monthly cost quota | The lowest of the account's own (none means no limit) and each tag's. |
| Concurrent requests | The lowest of the account's cap (its own override, else its role's) and each tag's, even for an unlimited role. Like the account's cap, it applies to each concurrency budget separately: ordinary requests, concurrency-exempt models and token counting. |
| Models | Only models on every tag's allowlist, on top of the role, disabled-model, key and project checks. A tag without an allowlist allows every model; allowlists that share no model allow none. |

A tag can only narrow what an account may do, so tags never conflict, and an automatic tag cannot loosen a limit an administrator set. To give a group more, raise the accounts' own limits.

The limits apply to API keys, inference grants from the cloud agent, requests made on a user's behalf (`X-On-Behalf-Of`) and the console playground. Batch work obeys a tag's quotas and allowlist, but its concurrency is the batch scheduler's own per-role limit. `/v1/models` and the console's model pickers hide models a tag doesn't allow, and `GET /v1/key` and the usage dashboard report the tag's concurrency cap. A change applies at once in the process that made it and within 30 seconds in the others, when their cached auth lookups expire.

Amounts are in the [ledger currency](accounting.md). A write names the currency it was read in and is refused (409) if the ledger currency has changed since. A quota stored in a currency the ledger has no rate for is enforced as 0 (no spend) until an administrator enters it again; the console flags such a tag.

## The automatic rule

A rule has a window of *N* hours (1–168) and a threshold of *K* active hours (1–*N*). Every ten minutes a job counts, for each account, the distinct rolling hours among the last *N* in which it has at least one row in `api_logs`, and gives the tag to every active account with at least *K*. Rows written by batch jobs don't count, because the gateway sends batch requests at its own pace. Service accounts are never matched, since they are unattended by design; to put a tag's limits on one, add the tag to it by hand. `unattended` defaults to 12 of 12.

**Preview matches** in the editor shows whom another rule would match before you save it: *20 of 24* catches fewer heavy human users, and *1 of 6* catches anyone active in the last six hours. **Run rule now** runs a tag's rule at once. Only one run happens at a time across replicas: the job holds a PostgreSQL advisory lock, and a replica that can't take it skips its turn.

The rule only adds members. Requests refused by the tag's own limits aren't logged, so a rule that also removed members would lift the limits from the very accounts they stopped. Removing a tag that has a rule keeps the membership as `dismissed`, which applies no limits; the rule gives the tag back only if the account's requests since the dismissal match again. Removing the dismissed marker as well (the × on a struck-through tag) forgets the dismissal, and adding the tag by hand makes any membership manual.

## Admin API

Every endpoint needs admin authentication (a console session or `ADMIN_TOKEN`), and every write is audited under a `user_tag.*` action.

| Method and path | What it does |
|---|---|
| `GET /admin/user-tags` | Every tag with its limits, rule and member counts, built-in tags first. |
| `POST /admin/user-tags` | Create a tag: `name` (lowercase letters, digits, `-` and `_`, starting with a letter or digit, at most 32), `description`, the limits and the rule. |
| `PATCH /admin/user-tags/{name}` | Change only the fields sent; `null` clears a limit. |
| `DELETE /admin/user-tags/{name}` | Delete a tag and its memberships. The built-in tag can't be deleted (409); turn its rule off instead. |
| `GET /admin/user-tags/{name}/members` | The memberships, applied ones first; `source` (repeatable) filters by `manual`, `auto` or `dismissed`. |
| `POST /admin/user-tags/{name}/members` | Add accounts by hand: `{"user_ids": [...]}`. Unknown ids are reported, not refused. |
| `DELETE /admin/user-tags/{name}/members/{user_id}` | Remove the tag from an account; under a rule, the membership is kept as dismissed. |
| `GET /admin/user-tags/{name}/preview` | The accounts the rule matches now, read-only. `window_hours` and `min_active_hours` try unsaved bounds. |
| `POST /admin/user-tags/{name}/detect` | Run the rule now (409 when it is off). |

The tag fields are `quota_daily_cost` and `quota_monthly_cost` (with `currency`), `max_concurrent_requests`, `allowed_models` (canonical model ids; an alias is stored as its model), `auto_enabled`, `auto_window_hours` and `auto_min_active_hours`. `GET /admin/users` accepts `tag=<name>` and returns each user's `tags`. `GET /admin/users/{id}/detail` returns the account's `tag_memberships` (dismissed ones included) and `tag_limits`, the combined limits of its applied tags.

## Storage

Migration `0023_user_tags` adds two tables and seeds `unattended`:

- `user_tags`: one row per tag with its limits (`NULL` for none) and rule; `system` marks a built-in tag.
- `user_tag_members`: `(user_id, tag)` with `source` (`manual`, `auto` or `dismissed`), `assigned_at` and `assigned_by` (the administrator's user id, or `NULL` for the rule). A membership is deleted with its account or its tag.

The auth lookups join an account's applied tags in the same query and fold their limits into the row ([`serving.user_tags`](https://github.com/HarvardMadSys/hybridInference/blob/dev/apps/backend/serving/user_tags.py)). For an account without tags this costs one primary-key probe.
