'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import type { ApiKeyListItem, ApiKeyUpdate } from '@/lib/api/user';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { useApiKeys, useProjects, useUpdateApiKey, useUsageStats } from '@/lib/hooks';
import { getErrorMessage } from '@/lib/utils/errors';
import { useMoney } from '@/lib/hooks/useMoney';
import { fill } from '@/lib/utils/interpolate';
import { Button } from '@/components/ui/Button';
import { InputField } from '@/components/ui/InputField';
import { useT } from '@/components/providers/useT';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { keyBudget, limitsSummary, parseBudget } from './keyLimits';
import { ModelPicker } from './ModelPicker';
import { projectHasLimits, projectSummary } from './ProjectDialogs';

const CARD = 'rounded-xl bg-white p-4 shadow-sm ring-1 ring-gray-200 sm:p-6';

function budgetText(value: number | null | undefined): string {
  return value == null ? '' : String(value);
}

/** Spend in one window, with a bar against the key's budget when it has one. */
function SpendMeter({
  label,
  spent,
  budget,
  currency,
}: {
  label: string;
  spent: number;
  budget: number | null | undefined;
  /** The ledger currency both amounts are in. */
  currency: 'USD' | 'CNY';
}): JSX.Element {
  const t = useT();
  const money = useMoney();
  const share = budget == null ? null : budget === 0 ? 1 : Math.min(spent / budget, 1);
  return (
    <div>
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-sm text-gray-600">{label}</span>
        <span className="text-sm font-medium text-gray-900">
          {budget == null
            ? money.format(spent, undefined, currency)
            : fill(t('dashboard.key_detail.spent_of', '{spent} of {budget}'), {
                spent: money.format(spent, undefined, currency),
                budget: money.format(budget, undefined, currency),
              })}
        </span>
      </div>
      {share != null && (
        <div
          className="mt-2 h-1.5 overflow-hidden rounded-full bg-gray-100"
          role="meter"
          aria-label={label}
          aria-valuemin={0}
          aria-valuemax={budget ?? 0}
          aria-valuenow={Math.min(spent, budget ?? spent)}
        >
          <div
            className={`h-full rounded-full ${share >= 1 ? 'bg-red-500' : share >= 0.8 ? 'bg-amber-500' : 'bg-gray-900'}`}
            style={{ width: `${share * 100}%` }}
          />
        </div>
      )}
    </div>
  );
}

/**
 * One key's own page: what it spent, and its limits on top of the account's
 * quota. Only an active key's limits can be changed.
 */
export function ApiKeyDetail({ keyPrefix }: { keyPrefix: string }): JSX.Element {
  const t = useT();
  const fmt = useFormatter();
  const money = useMoney();
  const { data, isLoading, error } = useApiKeys();
  const key = data?.keys.find((candidate) => candidate.key_prefix === keyPrefix);

  const back = (
    <Link href="/dashboard/keys" className="text-sm text-gray-500 hover:text-gray-900">
      ← {t('dashboard.key_detail.back', 'API Keys')}
    </Link>
  );

  if (isLoading) {
    return <div className="text-sm text-gray-500">{t('common.loading', 'Loading...')}</div>;
  }
  if (error || !key) {
    return (
      <div className="space-y-4">
        {back}
        <div className={CARD}>
          <h1 className="text-lg font-semibold text-gray-900">
            {t('dashboard.key_detail.not_found_title', 'API key not found')}
          </h1>
          <p className="mt-1 text-sm text-gray-600">
            {error
              ? t('dashboard.keys.load_failed', 'Failed to load API keys. Please try again later.')
              : t(
                  'dashboard.key_detail.not_found',
                  'This key does not exist or was regenerated under a new prefix. Find it in the list of your keys.',
                )}
          </p>
        </div>
      </div>
    );
  }

  const formatDate = (value?: string | null): string =>
    value
      ? fmt.date(value, { year: 'numeric', month: 'short', day: 'numeric' })
      : t('dashboard.keys.never', 'Never');

  return (
    <div className="space-y-6">
      {back}
      <div>
        <h1
          className={`text-2xl font-semibold tracking-tight ${key.name ? 'text-gray-900' : 'italic text-gray-400'}`}
        >
          {key.name ?? t('dashboard.keys.unnamed', 'Unnamed key')}
        </h1>
        <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-gray-500">
          <code className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-700">{key.key_masked}</code>
          <span>
            {fill(t('dashboard.key_detail.created', 'Created {date}'), {
              date: formatDate(key.created_at),
            })}
          </span>
          <span>
            {fill(t('dashboard.key_detail.last_used', 'Last used {date}'), {
              date: formatDate(key.last_used_at),
            })}
          </span>
          {key.status !== 'active' && (
            <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600">
              {key.status === 'revoked'
                ? t('dashboard.keys.status_revoked', 'revoked')
                : key.status}
            </span>
          )}
        </p>
      </div>

      <section className={CARD}>
        <h2 className="text-base font-semibold text-gray-900">
          {t('dashboard.key_detail.spend_title', 'Spend (UTC)')}
        </h2>
        <div className="mt-4 grid gap-5 sm:grid-cols-2">
          <SpendMeter
            label={t('dashboard.key_detail.today', 'Today')}
            spent={money.amount(key, 'spent_today') ?? 0}
            budget={keyBudget(key, 'budget_daily')}
            currency={money.sourceCurrency(key)}
          />
          <SpendMeter
            label={t('dashboard.key_detail.this_month', 'This month')}
            spent={money.amount(key, 'spent_month') ?? 0}
            budget={keyBudget(key, 'budget_monthly')}
            currency={money.sourceCurrency(key)}
          />
        </div>
        <p className="mt-4 text-xs text-gray-500">
          {t(
            'dashboard.key_detail.spend_note',
            'Counted since usage was first attributed to keys. Regenerating the key keeps its spend.',
          )}
        </p>
      </section>

      {key.status === 'active' ? (
        <KeyLimitsForm key={key.key_prefix} apiKey={key} />
      ) : (
        <section className={CARD}>
          <h2 className="text-base font-semibold text-gray-900">
            {t('dashboard.key_detail.limits_title', 'Limits')}
          </h2>
          <p className="mt-2 text-sm text-gray-600">{limitsSummary(key, t, money.value)}</p>
        </section>
      )}
    </div>
  );
}

function KeyLimitsForm({ apiKey }: { apiKey: ApiKeyListItem }): JSX.Element {
  const t = useT();
  const money = useMoney();
  const toast = useAccountToast();
  const update = useUpdateApiKey();
  const { data: usage } = useUsageStats('today');
  const { data: projects } = useProjects();
  const project = projects?.find((candidate) => candidate.id === apiKey.project_id);

  // Budgets are typed and sent in the ledger currency: a native amount on a
  // non-USD ledger, the `*_usd` field on a USD one.
  const currency = money.sourceCurrency(apiKey);
  const native = currency !== 'USD';
  const storedDaily = keyBudget(apiKey, 'budget_daily');
  const storedMonthly = keyBudget(apiKey, 'budget_monthly');
  const [daily, setDaily] = useState(budgetText(storedDaily));
  const [monthly, setMonthly] = useState(budgetText(storedMonthly));
  const [allowed, setAllowed] = useState<string[] | null>(apiKey.allowed_models ?? null);
  const [saveError, setSaveError] = useState<string | null>(null);

  // A refetch after saving brings the stored values back; start from them.
  useEffect(() => {
    setDaily(budgetText(storedDaily));
    setMonthly(budgetText(storedMonthly));
    setAllowed(apiKey.allowed_models ?? null);
  }, [storedDaily, storedMonthly, apiKey.allowed_models]);

  const maxBudget = native ? Infinity : undefined;
  // A budget left as stored is kept as stored, unvalidated: a migrated native
  // budget may carry more decimal places than a new one may be typed with.
  const dailyValue =
    daily === budgetText(storedDaily) ? storedDaily : parseBudget(daily, maxBudget);
  const monthlyValue =
    monthly === budgetText(storedMonthly) ? storedMonthly : parseBudget(monthly, maxBudget);
  const invalidBudget = fill(
    t(
      'dashboard.key_detail.budget_invalid',
      'Enter an amount in {currency}, such as 5 or 12.50, or leave it blank.',
    ),
    { currency },
  );
  const noModels = allowed !== null && allowed.length === 0;

  const changes: ApiKeyUpdate = {};
  if (dailyValue !== undefined && dailyValue !== storedDaily) {
    if (native) changes.budget_daily = dailyValue;
    else changes.budget_daily_usd = dailyValue;
  }
  if (monthlyValue !== undefined && monthlyValue !== storedMonthly) {
    if (native) changes.budget_monthly = monthlyValue;
    else changes.budget_monthly_usd = monthlyValue;
  }
  // A native amount names its currency, so the server can refuse a stale page.
  if (native && ('budget_daily' in changes || 'budget_monthly' in changes)) {
    changes.currency = currency;
  }
  const before = apiKey.allowed_models == null ? null : [...apiKey.allowed_models].sort();
  const nextAllowed = allowed == null ? null : [...allowed].sort();
  if (JSON.stringify(nextAllowed) !== JSON.stringify(before)) {
    changes.allowed_models = nextAllowed;
  }
  const canSave =
    dailyValue !== undefined &&
    monthlyValue !== undefined &&
    !noModels &&
    Object.keys(changes).length > 0 &&
    !update.isPending;

  async function save(): Promise<void> {
    if (!canSave) return;
    setSaveError(null);
    try {
      await update.mutateAsync({ keyPrefix: apiKey.key_prefix, changes });
      toast.success(t('dashboard.key_detail.saved_toast', 'Limits saved.'));
    } catch (err) {
      setSaveError(getErrorMessage(err));
    }
  }

  const accountQuota = usage ? money.amount(usage.quota, 'daily_limit') : null;

  return (
    <form
      className={`${CARD} space-y-6`}
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <div>
        <h2 className="text-base font-semibold text-gray-900">
          {t('dashboard.key_detail.limits_title', 'Limits')}
        </h2>
        <p className="mt-1 text-sm text-gray-500">
          {t(
            'dashboard.key_detail.limits_intro',
            'Limit what this key can spend and which models it can call. Requests over a limit are refused; your other keys are not affected.',
          )}
        </p>
        {project && projectHasLimits(project) && (
          <p className="mt-2 rounded-md bg-gray-50 px-3 py-2 text-sm text-gray-600 ring-1 ring-inset ring-gray-200">
            {fill(
              t(
                'dashboard.key_detail.project_limits',
                'Project {name} limits this key too, together with its other keys: {summary}.',
              ),
              { name: project.name, summary: projectSummary(project, t, money) },
            )}
          </p>
        )}
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <InputField
          label={fill(t('dashboard.key_detail.daily_budget', 'Daily budget ({currency})'), {
            currency,
          })}
          inputMode="decimal"
          placeholder={t('dashboard.key_detail.no_limit', 'No limit')}
          value={daily}
          onChange={(event) => setDaily(event.target.value)}
          error={dailyValue === undefined ? invalidBudget : undefined}
          hint={
            accountQuota != null
              ? fill(
                  t(
                    'dashboard.key_detail.account_quota_hint',
                    "Your account's daily quota of {amount} also applies.",
                  ),
                  { amount: money.value(usage?.quota, 'daily_limit') },
                )
              : undefined
          }
        />
        <InputField
          label={fill(t('dashboard.key_detail.monthly_budget', 'Monthly budget ({currency})'), {
            currency,
          })}
          inputMode="decimal"
          placeholder={t('dashboard.key_detail.no_limit', 'No limit')}
          value={monthly}
          onChange={(event) => setMonthly(event.target.value)}
          error={monthlyValue === undefined ? invalidBudget : undefined}
          hint={t('dashboard.key_detail.zero_hint', 'A budget of 0 pauses the key.')}
        />
      </div>

      <ModelPicker
        value={allowed}
        onChange={setAllowed}
        title={t('dashboard.key_detail.models_title', 'Models')}
        emptyMessage={t(
          'dashboard.key_detail.pick_a_model',
          'Pick at least one model, or allow all models. To stop the key, revoke it or set a budget of 0.',
        )}
        hint={t(
          'dashboard.key_detail.models_hint',
          'Other models answer this key as if they did not exist, and its model list shows only these.',
        )}
      />

      {saveError && (
        <p role="alert" className="text-sm text-red-600">
          {saveError}
        </p>
      )}
      <div className="flex justify-end">
        <Button type="submit" disabled={!canSave} isLoading={update.isPending}>
          {t('dashboard.key_detail.save', 'Save limits')}
        </Button>
      </div>
    </form>
  );
}
