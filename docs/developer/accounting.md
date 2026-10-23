# Accounting Currency

The gateway uses USD by default. A deployment can use CNY for model prices, usage charges and user quotas by migrating its PostgreSQL ledger and activating a matching distribution manifest. Changing the currency is an offline accounting operation, not a display preference.

## Currency and amount contracts

Primary database amounts use columns such as `cost_amount`, `quota_daily_cost_amount` and `total_cost_amount`, alongside `accounting_currency`. Existing `*_usd` columns remain USD equivalents. The migration preserves their historical values; subsequent writes maintain both representations using the persisted accounting policy.

Monetary APIs expose native fields such as `cost`, `daily_limit`, `quota_daily_cost` and `usage_today`, with `currency`. Model catalog responses expose native `prices` with `currency`; their legacy `pricing` dictionary remains USD. Role-quota settings expose `amount` with `currency`, while `value` remains USD. New clients should read these native fields directly and submit native monetary writes with the matching currency. Sending a different currency, or both a native amount and its USD alias, is rejected. Legacy `*_usd` inputs and outputs retain their USD meaning.

`GET /site-config` exposes `accounting.currency` for the console. The console formats the amounts supplied by the API without applying another exchange rate. A CNY response missing its native amount must not be displayed by relabelling its legacy USD value.

Provider balances and resource quotas retain the units reported by their provider. Provider-reported `upstream_cost_usd` and RouteWise USD budgets, candidate costs and diagnostics remain USD. Changing the deployment ledger does not change these contracts.

## Model price currency

In model configuration, `pricing_currency` declares the unit of `pricing` and `pricing_schedule`; it defaults to `USD`. Token prices are per million tokens, while `request` and `image` are per request and per image. A CNY deployment converts configured USD tariffs using its fixed accounting rate. Set `pricing_currency: CNY` only when the configured tariff is already in CNY; it is then used directly. A USD deployment rejects CNY tariffs.

Automatic DeepSeek pricing follows the accounting currency: USD uses the [official English pricing page](https://api-docs.deepseek.com/quick_start/pricing/), and CNY uses the [official Chinese pricing page](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/). CNY source prices are read directly, not converted from the English page. Snapshots are stored separately by currency and a mismatched snapshot is rejected. See [Automatic DeepSeek prices](adding-models.md#automatic-deepseek-prices) for source configuration and refresh behavior.

## Prepare an offline migration

Use `ops/accounting_currency.py` from the source checkout with the gateway's database connection configuration. Omitting `--apply` performs a read-only preview. CNY requires a positive `--usd-to-currency-rate`, an ISO `--rate-date` in `YYYY-MM-DD` form, and a nonempty `--rate-source` identifying the operator's rate source. The rate means CNY per USD. The command does not fetch an exchange rate.

Choose and record the rate as an accounting policy before the maintenance window. It converts existing usage and quotas and remains the fixed rate for USD compatibility values and USD-denominated model tariffs after migration. It is not a live FX feed and cannot be edited or applied again with a different rate after CNY activation.

Start with an initialized USD database, including for a new deployment with no usage. Prepare the compatible application version and a recoverable database backup, and retain the previous deployment configuration. Test the restore procedure before applying the migration. Do not expose traffic while preparing the CNY ledger.

Stop incoming traffic, drain in-flight requests, and stop every gateway worker, rollup, scheduled job and maintenance process that connects to the database. Disable automatic restarts and close database consoles, backup connections and other database clients. Keep them stopped until migration and configuration checks finish. The command refuses `--apply` when another client is connected; it does not terminate those clients, and its check does not prevent a scheduler from starting a new one.

Set `ACCOUNTING_RATE`, `ACCOUNTING_RATE_DATE` and `ACCOUNTING_RATE_SOURCE` to the approved values in your shell, then preview from the repository root:

```bash
PYTHONPATH=apps/backend uv run python ops/accounting_currency.py \
  --currency CNY \
  --usd-to-currency-rate "${ACCOUNTING_RATE:?Set the approved CNY per USD rate}" \
  --rate-date "${ACCOUNTING_RATE_DATE:?Set the approved ISO rate date}" \
  --rate-source "${ACCOUNTING_RATE_SOURCE:?Set the approved rate source}"
```

Review and retain the JSON output: previous and target policies, row counts, priced-row counts, original USD totals and proposed native totals for each monetary column. Reconcile each table separately; logs and their rollups describe overlapping usage and must not be added together as independent charges. The preview does not change the schema, amounts or policy.

## Apply and activate

With the backup available and all other database clients still stopped, run the same command with `--apply`:

```bash
PYTHONPATH=apps/backend uv run python ops/accounting_currency.py \
  --currency CNY \
  --usd-to-currency-rate "${ACCOUNTING_RATE:?Set the approved CNY per USD rate}" \
  --rate-date "${ACCOUNTING_RATE_DATE:?Set the approved ISO rate date}" \
  --rate-source "${ACCOUNTING_RATE_SOURCE:?Set the approved rate source}" \
  --apply
```

The command locks the affected tables and applies the conversion in one serializable transaction. It fills native amounts for request logs, API-key quotas, user daily costs, provider hourly statistics and RouteWise probe samples, where those tables exist. It records the policy and completion marker in `site_settings` and an audit record in `accounting_currency_migrations`. Retain the JSON result and confirm `applied` and `migration_complete` are true. Repeating the exact completed policy returns `already_applied` without converting again.

After a successful migration, add this top-level section to the deployment's active manifest:

```yaml
accounting:
  currency: CNY
```

The manifest selects the expected currency only; the rate, date and source belong to the persisted migration policy. Activate the manifest using the existing [distribution configuration](distribution-customization.md#activating-a-manifest) and start the compatible gateway. Startup rejects a manifest/database currency mismatch, an invalid policy or a CNY policy without a matching completed migration. Setting `accounting.currency: CNY` alone does not migrate a database.

Before reopening traffic, verify that `/site-config` reports CNY, model catalog `prices` and usage/quota fields carry CNY, and the console displays those native values. Check a controlled request and quota update against the ledger and its USD equivalents. Then resume the remaining workers and scheduled jobs. Keep the pre-migration backup and the migration evidence according to the deployment's retention policy.

## Rollback

A failed apply rolls back its database transaction. Keep services stopped, inspect the error and persisted policy, then correct the problem before retrying with the same approved inputs.

An applied CNY policy cannot be converted back to USD in place or replaced with a different rate. Rollback requires stopping all clients, restoring the pre-migration database backup and restoring the matching previous deployment configuration before restarting. Plan how to preserve or reconcile any writes accepted after the backup; restoring it does not retain those writes. Do not simulate rollback by changing the manifest, editing the policy rows or applying an inverse rate.
