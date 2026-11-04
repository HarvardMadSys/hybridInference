'use client';

import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';
import type { ApiKeyListItem, ApiKeyUpdate } from '@/lib/api/user';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { useApiKeys, useModels, useUpdateApiKey, useUsageStats } from '@/lib/hooks';
import { getErrorMessage } from '@/lib/utils/errors';
import { formatUsd } from '@/lib/utils/currency';
import { fill } from '@/lib/utils/interpolate';
import { Button } from '@/components/ui/Button';
import { InputField } from '@/components/ui/InputField';
import { useT } from '@/components/providers/useT';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { limitsSummary, parseBudget } from './keyLimits';

const CARD = 'rounded-xl bg-white p-6 shadow-sm ring-1 ring-gray-200';

function budgetText(value: number | null | undefined): string {
  return value == null ? '' : String(value);
}

/** Spend in one window, with a bar against the key's budget when it has one. */
function SpendMeter({
  label,
  spent,
  budget,
}: {
  label: string;
  spent: number;
  budget: number | null | undefined;
}): JSX.Element {
  const t = useT();
  const share = budget == null ? null : budget === 0 ? 1 : Math.min(spent / budget, 1);
  return (
    <div>
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-sm text-gray-600">{label}</span>
        <span className="text-sm font-medium text-gray-900">
          {budget == null
            ? formatUsd(spent)
            : fill(t('dashboard.key_detail.spent_of', '{spent} of {budget}'), {
                spent: formatUsd(spent),
                budget: formatUsd(budget),
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
            spent={key.spent_today_usd ?? 0}
            budget={key.budget_daily_usd}
          />
          <SpendMeter
            label={t('dashboard.key_detail.this_month', 'This month')}
            spent={key.spent_month_usd ?? 0}
            budget={key.budget_monthly_usd}
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
          <p className="mt-2 text-sm text-gray-600">{limitsSummary(key, t)}</p>
        </section>
      )}
    </div>
  );
}

function KeyLimitsForm({ apiKey }: { apiKey: ApiKeyListItem }): JSX.Element {
  const t = useT();
  const toast = useAccountToast();
  const update = useUpdateApiKey();
  const { data: models, isLoading: modelsLoading } = useModels();
  const { data: usage } = useUsageStats('today');

  const [daily, setDaily] = useState(budgetText(apiKey.budget_daily_usd));
  const [monthly, setMonthly] = useState(budgetText(apiKey.budget_monthly_usd));
  const [restricted, setRestricted] = useState(apiKey.allowed_models != null);
  const [selected, setSelected] = useState<Set<string>>(new Set(apiKey.allowed_models ?? []));
  const [saveError, setSaveError] = useState<string | null>(null);

  // A refetch after saving brings the stored values back; start from them.
  useEffect(() => {
    setDaily(budgetText(apiKey.budget_daily_usd));
    setMonthly(budgetText(apiKey.budget_monthly_usd));
    setRestricted(apiKey.allowed_models != null);
    setSelected(new Set(apiKey.allowed_models ?? []));
  }, [apiKey.budget_daily_usd, apiKey.budget_monthly_usd, apiKey.allowed_models]);

  // Models the key allows but the catalog no longer lists stay visible, so a
  // save does not drop them without the owner seeing it.
  const choices = useMemo(() => {
    const listed = (models?.data ?? []).map((model) => ({ id: model.id, name: model.name }));
    const known = new Set(listed.map((model) => model.id));
    const missing = (apiKey.allowed_models ?? [])
      .filter((id) => !known.has(id))
      .map((id) => ({ id, name: id, unavailable: true }));
    return [...listed.map((model) => ({ ...model, unavailable: false })), ...missing];
  }, [models, apiKey.allowed_models]);

  const dailyValue = parseBudget(daily);
  const monthlyValue = parseBudget(monthly);
  const invalidBudget = t(
    'dashboard.key_detail.budget_invalid',
    'Enter an amount in USD, such as 5 or 12.50, or leave it blank.',
  );
  const noModels = restricted && selected.size === 0;
  const allowed = restricted ? [...selected].sort() : null;

  const changes: ApiKeyUpdate = {};
  if (dailyValue !== undefined && dailyValue !== (apiKey.budget_daily_usd ?? null)) {
    changes.budget_daily_usd = dailyValue;
  }
  if (monthlyValue !== undefined && monthlyValue !== (apiKey.budget_monthly_usd ?? null)) {
    changes.budget_monthly_usd = monthlyValue;
  }
  const before = apiKey.allowed_models == null ? null : [...apiKey.allowed_models].sort();
  if (JSON.stringify(allowed) !== JSON.stringify(before)) {
    changes.allowed_models = allowed;
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

  function toggle(id: string): void {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const accountQuota = usage?.quota.daily_limit_usd;

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
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <InputField
          label={t('dashboard.key_detail.daily_budget', 'Daily budget (USD)')}
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
                  { amount: formatUsd(accountQuota) },
                )
              : undefined
          }
        />
        <InputField
          label={t('dashboard.key_detail.monthly_budget', 'Monthly budget (USD)')}
          inputMode="decimal"
          placeholder={t('dashboard.key_detail.no_limit', 'No limit')}
          value={monthly}
          onChange={(event) => setMonthly(event.target.value)}
          error={monthlyValue === undefined ? invalidBudget : undefined}
          hint={t('dashboard.key_detail.zero_hint', 'A budget of 0 pauses the key.')}
        />
      </div>

      <fieldset>
        <legend className="text-sm font-medium text-gray-700">
          {t('dashboard.key_detail.models_title', 'Models')}
        </legend>
        <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:gap-6">
          <label className="flex items-center gap-2 text-sm text-gray-700">
            <input
              type="radio"
              name="model-access"
              checked={!restricted}
              onChange={() => setRestricted(false)}
            />
            {t('dashboard.key_detail.all_models', 'All models')}
          </label>
          <label className="flex items-center gap-2 text-sm text-gray-700">
            <input
              type="radio"
              name="model-access"
              checked={restricted}
              onChange={() => setRestricted(true)}
            />
            {t('dashboard.key_detail.some_models', 'Only the models selected below')}
          </label>
        </div>
        {restricted && (
          <div className="mt-3 max-h-72 overflow-y-auto rounded-lg ring-1 ring-gray-200">
            {modelsLoading ? (
              <p className="p-3 text-sm text-gray-500">{t('common.loading', 'Loading...')}</p>
            ) : (
              <ul className="divide-y divide-gray-100">
                {choices.map((model) => (
                  <li key={model.id}>
                    <label className="flex items-center gap-3 px-3 py-2 text-sm hover:bg-gray-50">
                      <input
                        type="checkbox"
                        checked={selected.has(model.id)}
                        onChange={() => toggle(model.id)}
                      />
                      <span className="min-w-0 flex-1 truncate text-gray-900">{model.name}</span>
                      <code className="truncate text-xs text-gray-500">{model.id}</code>
                      {model.unavailable && (
                        <span className="text-xs text-amber-700">
                          {t('dashboard.key_detail.unavailable', 'no longer available')}
                        </span>
                      )}
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {noModels && (
          <p className="mt-2 text-xs text-red-600">
            {t(
              'dashboard.key_detail.pick_a_model',
              'Pick at least one model, or allow all models. To stop the key, revoke it or set a budget of 0.',
            )}
          </p>
        )}
        {restricted && (
          <p className="mt-2 text-xs text-gray-500">
            {t(
              'dashboard.key_detail.models_hint',
              'Other models answer this key as if they did not exist, and its model list shows only these.',
            )}
          </p>
        )}
      </fieldset>

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
