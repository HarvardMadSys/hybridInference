# Accounting Currency

The gateway keeps its ledger in USD by default. A deployment can keep it in CNY instead: model prices, usage charges and user quotas are then counted in CNY. Each deployment has exactly one ledger currency. It is chosen by an offline, audited migration and cannot be changed afterwards; it is not a display preference, and neither editing the site configuration nor restarting the gateway changes it.

A deployment that never runs the migration is unaffected: it charges, rounds, admits and refuses exactly as before this feature existed, and its APIs answer with exactly the same fields.

## The rules every amount follows

`serving.accounting.AccountingPolicy` is the single place that knows how the ledger currency changes the arithmetic. Business code asks the policy; it never compares currency codes.

| | USD ledger (default) | CNY ledger |
|---|---|---|
| Record of the ledger | the `*_usd` columns, as always | the native `*_amount` columns (`NUMERIC(24, 12)`) |
| `*_usd` columns | the ledger itself | USD equivalents at the fixed policy rate, kept by the `accounting_sync_amounts` triggers |
| Arithmetic of a charge, a gate and a reservation | binary floats, unchanged | exact decimals of the configured prices |
| One counter or reservation increment rounds to | `0.000001` (half away from zero) | `0.000000000001` (half away from zero) |
| Request log `cost_usd` | `DECIMAL(12, 8)`, unchanged | the USD equivalent of `cost_amount` |
| Flat estimate, reservation ceiling, per-request ceiling | `0.01`, `5`, `1000` USD | the same USD constants at the fixed rate |

A request's cost is computed once from its prices and its usage, and rounded only where it is counted. On a CNY ledger a price far below a cent per token accumulates exactly: twelve decimal places hold a ten-thousandth of a fen per million tokens. A CNY amount is never converted to USD and back. The `*_usd` equivalents are derived from it, one way, for USD contracts only.

The rate (`usd_to_currency_rate`, CNY per USD) is fixed when the ledger is migrated, with the date and source the operator recorded. It is not a live exchange rate. An amount once recorded keeps its value: nothing re-reads history at another rate.

## Model price currency

`pricing_currency` declares the currency of a model's or a route's `pricing` and `pricing_schedule`; it defaults to `USD`. Token prices are per million tokens, `request` per request and `image` per image.

- A price in the ledger currency is charged as written.
- A USD price on a CNY ledger is converted once, at the fixed rate.
- Any other combination, such as a CNY price on a USD ledger, is refused: there is no declared rate for it.

Automatic DeepSeek prices follow the ledger: a USD ledger reads the [official English pricing page](https://api-docs.deepseek.com/quick_start/pricing/) and a CNY ledger the [official Chinese pricing page](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/), each in its own currency and never converted from the other. Snapshots are stored by currency, and a snapshot in another currency than the ledger's is refused. See [Automatic DeepSeek prices](adding-models.md#automatic-deepseek-prices).

## API contracts

Every existing `*_usd` field keeps its USD meaning in every deployment.

- **USD ledger.** Responses keep exactly their fields: no `currency` and no native amounts are added. The one addition is two headers on a budget refusal (429): `X-Accounting-Currency: USD` and `X-RateLimit-Cost-Currency: USD`, which state the unit of the `X-RateLimit-*-Cost` headers.
- **CNY ledger.** Responses add native amounts beside their USD fields, with `currency`: `cost`, `quota`, `spent`, `remaining`, `daily_limit`, `budget_daily`, `usage_today` and the like, as exact decimals. The `*_usd` fields carry the USD equivalents. The model catalog adds `prices` and `currency`; its `pricing` stays USD.
- **Writes.** A native amount is sent with the ledger `currency`, for example `{"budget_daily": "50", "currency": "CNY"}`; the legacy `budget_daily_usd` still means USD. Sending a currency other than the ledger's, or both a native amount and its USD alias, is refused. A native write is stored exactly, and its USD equivalent beside it.

USD-only contracts stay USD: the OpenAI-compatible billing endpoints (`/v1/usage`, `/v1/dashboard/billing/*`), `/internal/agent-grants/{id}/usage` (`spent_usd`), batch `cost_usd` totals, `upstream_cost_usd`, RouteWise budgets and diagnostics, and the `X-RateLimit-*-Cost` headers. On a CNY ledger they report USD equivalents, so clients such as new-api, one-api, sub2api and the cloud agent keep working unchanged. Provider balances and quotas keep the units their provider reports.

The role default quotas (`user_daily_quota_<role>`) are runtime settings stored in USD. On a CNY ledger Admin shows and accepts them in CNY, and the stored value is the USD equivalent of what was entered.

`GET /site-config` reports `accounting.currency` so the console can format amounts. The console displays the amounts the API supplies; it never applies a rate of its own, and it never shows a USD value relabelled as CNY.

## Every way a request is charged

Chat completions, streaming and non-streaming, the Responses API, Anthropic `/v1/messages`, embeddings, the console's web chat (playground), batches, inference grants and requests on behalf of another user are all charged at one billing point, in the ledger currency, through the same spend ledger:

- **Admission.** The request is checked against the API key's budgets, then its project's, then the account's daily and monthly quotas, and reserves an estimate (between the flat minimum and the reservation ceiling). On a CNY ledger every one of these comparisons is between native amounts.
- **Charge.** The finished request's cost is recorded in the ledger and written to the account, key and project counters.
- **Failure and cancellation.** These are charged exactly as before: what the billing point recorded, and nothing for a request that produced no billable usage.

## Shared accounting and draining

The local (per-process) and the shared accounting follow the same rules. In the shared mode, reservations and reconciliations are kept in `spend_reservations.amount` and `spend_reconciliations.amount`, in the ledger currency, beside their USD equivalents. A settlement's digest includes the currency and the rate, so a batch resent after its answer was lost can never be counted in another unit. A log row records the currency of its charge (`metadata.spend_currency`), and a crash reconciliation refuses rows recorded in another currency.

Each process reports its accounting in its `config_instances` capabilities and in its handoff status: protocol `2`, the mode, the currency and the rate. `python -m serving.shared_spend status` lists as `unsupported` every process that reports an older protocol, or another currency or rate (`other_policy`): processes that cannot keep the same ledger must not take part in it, whatever mode they report. A process that still holds a cost not known to be written, in either mode, cannot be sealed for a handoff.

## Migrating a ledger

### An existing USD database

`python -m migrations upgrade` with this release applies `0022_ledger_currency`. It only adds the native columns, their triggers, the dashboard rollup's native sum and the migration audit table; it converts nothing. The previous release keeps running on the upgraded schema, and its writes get their native amounts from the triggers. The database stays a USD ledger until an operator migrates it.

### A new deployment

Run the schema migrations as for any deployment. A new USD deployment needs nothing more. A new CNY deployment runs the currency migration below before it serves traffic. With no usage there is nothing to convert, but the policy, its completion marker and the audit record are written in the same way.

### Moving a ledger to CNY

The currency migration is `ops/accounting_currency.py`. It issues no DDL and refuses `--apply` unless:

- the schema is at this release's head and has the native columns, triggers and audit table;
- no other client is connected to the database;
- the shared accounting is in its `local` mode, with no shared reservations, settlements or reconciliations.

CNY requires a positive `--usd-to-currency-rate` (CNY per USD), an ISO `--rate-date` and a nonempty `--rate-source`. The command does not fetch a rate.

Before the maintenance window:

1. Choose and record the rate.
2. Take a database backup and test the restore.
3. Keep the previous deployment configuration.

During the window:

1. Stop incoming traffic and drain in-flight requests.
2. Stop every gateway worker, rollup, scheduled job, maintenance process, database console and backup connection, and disable automatic restarts. The command refuses to apply while another client is connected; it terminates nobody, and it cannot stop a scheduler from starting a new client afterwards.

Then preview, from the repository root:

```bash
PYTHONPATH=apps/backend uv run python ops/accounting_currency.py \
  --currency CNY \
  --usd-to-currency-rate "${ACCOUNTING_RATE:?Set the approved CNY per USD rate}" \
  --rate-date "${ACCOUNTING_RATE_DATE:?Set the approved ISO rate date}" \
  --rate-source "${ACCOUNTING_RATE_SOURCE:?Set the approved rate source}"
```

The preview is read-only. It shows the previous and target policies and, for each monetary column, the row counts, the original USD total and the proposed native total. Logs and their rollups describe overlapping usage; reconcile each table on its own.

Run the same command with `--apply`. In one serializable transaction it:

- fills the native amounts of every monetary table at the rate;
- records the policy and a completion marker in `site_settings` (`accounting:policy`, `accounting:migration`);
- writes an audit row to `accounting_currency_migrations`;
- raises the schema's compatibility floor to `0022_ledger_currency`.

Keep the JSON result and confirm `applied` and `migration_complete` are true. The `*_usd` values are left as they were. Repeating the exact completed policy returns `already_applied` and changes nothing: no amount is converted twice and the floor is not touched. Any other policy, a different rate included, is refused once a CNY policy is active.

Then declare the currency in `SITE_CONFIGURATION` before the gateway starts:

```json
{"schema_version": 1, "distribution": {"id": "<id>"}, "accounting": {"currency": "CNY"}}
```

For example, `python -m serving.config.manage set SITE_CONFIGURATION '<json>'` with the backend image. Startup refuses all of these:

- a declaration that differs from the ledger's currency (an absent declaration means USD);
- an invalid policy, or a CNY policy without its matching completion marker;
- a database missing the native columns, triggers or audit table.

Once the gateway runs, Admin refuses a site configuration whose currency differs from the ledger's. Setting the declaration alone never migrates a database.

Before reopening traffic:

1. Check that `/site-config`, the model catalog `prices` and the usage and quota fields carry CNY.
2. Check a controlled request and a quota change against the ledger and its USD equivalents.

## Compatibility and rollback

| Database | Images that start on it |
|---|---|
| USD, at `0022_ledger_currency` | this release and the previous one (the revision only expands the schema) |
| CNY, migrated by `ops/accounting_currency.py` | this release and later ones; older images refuse to start, because the compatibility floor is `0022_ledger_currency` |

An image older than `0022_ledger_currency` knows only a USD ledger. It would charge a CNY tariff as dollars, which is why the migration shuts it out. A failed `--apply` rolls back its transaction: keep services stopped, correct the cause, and run it again with the same approved inputs. An applied CNY policy cannot be converted back in place or given another rate. Going back means stopping every client, restoring the pre-migration backup and the matching previous configuration, and accounting for the writes accepted after the backup. Do not simulate a rollback by editing the declaration or the policy rows, or by applying an inverse rate. No schema downgrade is involved: `0022_ledger_currency`'s downgrade keeps every column.

## Databases built by a distribution

A distribution may already keep a CNY ledger whose own revisions created these objects, with the same `accounting_sync_amounts` function, the same columns and the same triggers. For such a database:

- **Keep its released revisions.** Keep the files, ids and parents of its own revisions, so that a database that ran them never runs them again.
- **Join its head to `0022_ledger_currency`.** Add one empty merge revision: `down_revision = ("<distribution head>", "0022_ledger_currency")`, with no upgrade body.
- **Upgrade.** `0022_ledger_currency` then runs on that database as a no-op, except that it replaces the two `account_quotas` and `organizations` bridge functions with its own; their native branch is the one the distribution installed. It reads the function digest, `SYNC_FUNCTION_SHA256`, adds only what is missing, and converts no amount. A CNY database whose shared reservations or reconciliations lack native amounts is refused rather than relabelled.
- **Leave the floor as it is.** The compatibility floor is not raised, so the distribution's previous image keeps starting through its rollback window.
- **Keep the currency declared.** Keep `accounting.currency` in `SITE_CONFIGURATION`.
