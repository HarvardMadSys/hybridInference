# Accounting Currency

The gateway keeps its ledger in USD unless an offline, audited migration moves it to CNY. A CNY ledger counts model prices, charges and quotas in CNY. The currency cannot change after the migration, and editing the site configuration or restarting the gateway does not change it. A deployment that never migrates keeps USD, and its API responses carry no currency fields.

## How amounts are kept

`serving.accounting.AccountingPolicy` holds every rule that depends on the currency. Code asks the policy instead of comparing currency codes.

| | USD ledger (default) | CNY ledger |
|---|---|---|
| Ledger columns | the `*_usd` columns | the native `*_amount` columns (`NUMERIC(24, 12)`) |
| `*_usd` columns | the ledger itself | USD equivalents at the fixed rate, kept by the `accounting_sync_amounts` triggers |
| Arithmetic of a charge, a gate and a reservation | binary floats | exact decimals of the configured prices |
| One counter or reservation increment rounds to | `0.000001` (half away from zero) | `0.000000000001` (half away from zero) |
| Request log `cost_usd` | `DECIMAL(12, 8)` | the USD equivalent of `cost_amount` |
| Flat estimate, reservation ceiling, per-request ceiling | `0.01`, `5`, `1000` USD | the same USD constants at the fixed rate |

A request's cost is computed once, from its prices and usage, and rounded only where it is counted, so sub-cent token prices add up exactly on a CNY ledger. CNY amounts are never converted to USD and back; the `*_usd` equivalents are derived one way, for USD contracts. The rate (`usd_to_currency_rate`, CNY per USD) is fixed at migration time, with the date and source the operator recorded. It is not a live exchange rate, and recorded amounts are never re-read at another rate.

## Model prices

`pricing_currency` (default `USD`) is the currency of a model's or route's `pricing` and `pricing_schedule`. Token prices are per million tokens, `request` per request and `image` per image. A price in the ledger currency is charged as written, and a USD price on a CNY ledger is converted at the fixed rate. Any other combination, such as a CNY price on a USD ledger, is refused.

Automatic DeepSeek prices come from the [English pricing page](https://api-docs.deepseek.com/quick_start/pricing/) on a USD ledger and the [Chinese pricing page](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/) on a CNY ledger, never converted. A price snapshot in another currency than the ledger's is refused. See [Automatic DeepSeek prices](adding-models.md#automatic-deepseek-prices).

## API contracts

Every `*_usd` field means USD on every ledger.

- USD ledger: responses have no `currency` field and no native amounts. A budget refusal (429) adds `X-Accounting-Currency: USD` and `X-RateLimit-Cost-Currency: USD`, which give the unit of the `X-RateLimit-*-Cost` headers.
- CNY ledger: responses add exact native amounts with `currency` beside their USD fields (`cost`, `quota`, `spent`, `remaining`, `daily_limit`, `budget_daily`, `usage_today` and the like), and the `*_usd` fields carry USD equivalents. The model catalog adds `prices` and `currency`; its `pricing` stays USD.
- Writes: send a native amount with the ledger `currency`, for example `{"budget_daily": "50", "currency": "CNY"}`. `budget_daily_usd` still means USD. Another currency, or both a native amount and its USD alias, is refused.

These contracts stay USD on every ledger, so clients such as new-api, one-api, sub2api and the cloud agent work unchanged: the OpenAI-compatible billing endpoints (`/v1/usage`, `/v1/dashboard/billing/*`), `/internal/agent-grants/{id}/usage` (`spent_usd`), batch `cost_usd`, `upstream_cost_usd`, RouteWise budgets and diagnostics, and the `X-RateLimit-*-Cost` headers. Provider balances keep their provider's units.

The role default quotas (`user_daily_quota_<role>`) are stored in USD; on a CNY ledger Admin shows and accepts them in CNY. `GET /site-config` reports `accounting.currency`. The console shows the amounts the API supplies and never converts or relabels them.

## Charging

Every request is charged at one billing point, in the ledger currency, through one spend ledger: chat completions, Responses, Anthropic `/v1/messages`, embeddings, the console playground, batches, inference grants and requests made on behalf of another user. Admission checks the API key's budgets, then its project's budgets, then the account's daily and monthly quotas, and reserves an estimate between the flat minimum and the reservation ceiling. The finished cost goes to the account, key and project counters. A failed or cancelled request is charged what the billing point recorded, or nothing if it produced no billable usage.

Projects, multiple API keys and per-key budgets are described in the design record {download}`2026-11-01-projects-and-api-keys-design.md <../agents/specs/2026-11-01-projects-and-api-keys-design.md>`. [User tags](user-tags.md) can lower an account's quotas further.

## Shared accounting

In shared mode, reservations and reconciliations are kept in `spend_reservations.amount` and `spend_reconciliations.amount`, in the ledger currency, with USD equivalents. A settlement's digest includes the currency and the rate, and a crash reconciliation refuses log rows charged in another currency (`metadata.spend_currency`).

Each process reports protocol `2`, its mode, currency and rate in its `config_instances` capabilities. `python -m serving.shared_spend status` lists a process with an older protocol, or another currency or rate, as `unsupported` (and under `other_policy`); such a process must not join the ledger. A process that holds a cost not known to be written cannot be sealed for a handoff.

## Migrating to CNY

`python -m migrations upgrade` applies `0022_ledger_currency`. It adds the native columns, their triggers, the dashboard rollup's native sum and the audit table, and converts nothing. The previous release keeps running on it, and the database stays a USD ledger. A new CNY deployment runs the steps below before it serves traffic.

`ops/accounting_currency.py` converts the ledger. It issues no DDL, and it refuses `--apply` unless the schema is at this release's head, no other client is connected, and shared accounting is in `local` mode with no shared reservations, settlements or reconciliations. It needs a positive `--usd-to-currency-rate`, an ISO `--rate-date` and a nonempty `--rate-source`; it does not fetch a rate.

1. Before the maintenance window, choose and record the rate, take a database backup and test the restore, and keep the previous configuration.
2. Stop incoming traffic and drain in-flight requests. Stop every gateway worker, rollup, scheduled job, maintenance process, database console and backup connection, and disable automatic restarts. The command terminates no connection and cannot stop a scheduler from opening a new one.
3. Preview, from the repository root:

   ```bash
   PYTHONPATH=apps/backend uv run python ops/accounting_currency.py \
     --currency CNY \
     --usd-to-currency-rate "${ACCOUNTING_RATE:?Set the approved CNY per USD rate}" \
     --rate-date "${ACCOUNTING_RATE_DATE:?Set the approved ISO rate date}" \
     --rate-source "${ACCOUNTING_RATE_SOURCE:?Set the approved rate source}"
   ```

   The preview is read-only. It shows both policies and, per monetary column, the row counts, the USD total and the proposed native total. Logs and their rollups describe overlapping usage, so reconcile each table on its own.
4. Run the same command with `--apply`. In one serializable transaction it fills every native amount at the rate, records `accounting:policy` and `accounting:migration` in `site_settings`, writes an audit row to `accounting_currency_migrations`, and raises the compatibility floor to `0022_ledger_currency`. Keep the JSON result and check that `applied` and `migration_complete` are true. The `*_usd` values are not touched. Running the same policy again returns `already_applied` and changes nothing; any other policy, including another rate, is refused.
5. Before the gateway starts, declare the currency in `SITE_CONFIGURATION`, for example with `python -m serving.config.manage set SITE_CONFIGURATION '<json>'` in the backend image:

   ```json
   {"schema_version": 1, "distribution": {"id": "<id>"}, "accounting": {"currency": "CNY"}}
   ```

   Startup refuses a declaration that differs from the ledger (an absent one means USD), an invalid policy, a CNY policy without its completion marker, and a database missing the native columns, triggers or audit table. Once the gateway runs, Admin refuses a currency change. The declaration alone never migrates a database.
6. Before reopening traffic, check that `/site-config`, the catalog `prices` and the usage and quota fields carry CNY, and check one controlled request and one quota change against the ledger.

## Compatibility and rollback

| Database | Images that start on it |
|---|---|
| USD, at `0022_ledger_currency` | this release and the previous one (the revision only expands the schema) |
| CNY, migrated by `ops/accounting_currency.py` | this release and later ones; older images refuse to start, because the compatibility floor is `0022_ledger_currency` |

Older images know only USD and would charge a CNY tariff as dollars, which the floor prevents. A failed `--apply` rolls back its transaction: keep services stopped, fix the cause and run it again with the same inputs.

An applied CNY policy cannot be reversed or given another rate in place. To go back, stop every client, restore the pre-migration backup and the previous configuration, and account for the writes accepted after the backup. Never imitate a rollback by editing the declaration or the policy rows, or by applying an inverse rate. No schema downgrade is involved: the downgrade of `0022_ledger_currency` keeps every column.

## Databases built by a distribution

If a distribution's own revisions already created these objects, with the same `accounting_sync_amounts` function, columns and triggers:

1. Keep its released revisions (files, ids and parents), so a database that ran them never runs them again.
2. Add one empty merge revision: `down_revision = ("<distribution head>", "0022_ledger_currency")`.
3. Upgrade. `0022_ledger_currency` checks the function digest (`SYNC_FUNCTION_SHA256`), adds only what is missing, converts no amount, and replaces only the `account_quotas` and `organizations` bridge functions. It refuses a CNY database whose shared reservations or reconciliations lack native amounts. It does not raise the compatibility floor, so the distribution's previous image keeps starting.
4. Keep `accounting.currency` declared in `SITE_CONFIGURATION`.
