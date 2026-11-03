'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import clsx from 'clsx';
import { useUsageStats } from '@/lib/hooks';
import { useBranding } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { fill } from '@/lib/utils/interpolate';
import { Select } from '@/components/ui/Select';
import { popoverContainer, useAnchoredPopover } from '@/components/ui/anchoredPopover';

function formatUsd(amount: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(amount);
}

const RESET_AT_FORMAT: Intl.DateTimeFormatOptions = {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  timeZoneName: 'short',
};

/** `part / whole` within [0, 1]; 0 when there is no whole. */
function share(part: number, whole: number): number {
  return whole > 0 ? Math.min(Math.max(part / whole, 0), 1) : 0;
}

/**
 * The help button on a statistic's label. A press (or a tap, which a hover
 * tooltip never gets) shows the explanation; another press, Escape, or a press
 * or focus elsewhere hides it. `useAnchoredPopover` keeps it on screen.
 */
function HelpTip({ label, children }: { label: string; children: React.ReactNode }): JSX.Element {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const id = useId();
  const position = useAnchoredPopover(open, triggerRef, popoverRef, {
    width: 272,
    preferredHeight: 160,
  });

  useEffect(() => {
    if (!open) return;
    const outside = (event: Event) => {
      const target = event.target;
      if (
        target instanceof Node &&
        !triggerRef.current?.contains(target) &&
        !popoverRef.current?.contains(target)
      ) {
        setOpen(false);
      }
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('focusin', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('focusin', outside);
      document.removeEventListener('keydown', escape);
    };
  }, [open]);

  const container = open ? popoverContainer(triggerRef.current) : null;
  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        aria-label={label}
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-describedby={open ? id : undefined}
        onClick={() => setOpen((current) => !current)}
        className="-m-1 rounded-full p-1 text-gray-400 transition-colors hover:text-gray-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
      >
        <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
          />
        </svg>
      </button>
      {open && container
        ? createPortal(
            <div
              ref={popoverRef}
              id={id}
              role="tooltip"
              style={position}
              className="z-50 overflow-y-auto rounded-md bg-gray-900 px-3 py-2 text-xs leading-relaxed text-white shadow-lg"
            >
              {children}
            </div>,
            container,
          )
        : null}
    </>
  );
}

/** A thin bar showing `value` (0–1). Decorative: the figure beside it says the same. */
function ShareBar({ value }: { value: number }): JSX.Element {
  return (
    <div aria-hidden="true" className="h-1.5 overflow-hidden rounded-full bg-blue-100">
      <div className="h-full rounded-full bg-blue-600" style={{ width: `${value * 100}%` }} />
    </div>
  );
}

/** A secondary figure in the shared token breakdown. */
function Detail({ label, children }: { label: string; children: React.ReactNode }): JSX.Element {
  return (
    <div className="min-w-0">
      <span className="block text-xs text-gray-500">{label}</span>
      <span className="mt-1 block break-words text-sm font-medium tabular-nums text-gray-700">
        {children}
      </span>
    </div>
  );
}

function StatMetric({
  label,
  helpLabel,
  help,
  value,
  compact,
}: {
  label: string;
  helpLabel: string;
  help: React.ReactNode;
  value: string;
  /** One size smaller, for a row holding a figure too long for the usual size. */
  compact: boolean;
}): JSX.Element {
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-1.5 text-xs font-medium text-gray-500">
        <span>{label}</span>
        <HelpTip label={helpLabel}>{help}</HelpTip>
      </div>
      <div
        className={clsx(
          'mt-1.5 break-words font-semibold tabular-nums tracking-tight text-gray-900',
          compact ? 'text-lg sm:text-xl' : 'text-2xl sm:text-3xl',
        )}
      >
        {value}
      </div>
    </div>
  );
}

export function UsageStats(): JSX.Element {
  const branding = useBranding();
  const [period, setPeriod] = useState<'today' | 'month' | 'all'>('today');
  const { data: stats, isLoading, error } = useUsageStats(period);
  const quota = stats?.quota;
  const contactEmail = quota?.contact_email ?? branding.contactEmail;
  const t = useT();
  const fmt = useFormatter();
  const helpLabel = (label: string) =>
    fill(t('dashboard.usage.help_label', 'About {label}'), { label });

  const promptTokens = stats?.usage.prompt_tokens ?? 0;
  const cacheReadTokens = stats?.usage.cache_read_tokens ?? 0;
  const cacheWriteTokens = stats?.usage.cache_write_tokens ?? 0;
  const reasoningTokens = stats?.usage.reasoning_tokens ?? 0;
  // Cache reads are a subset of input tokens; clamp in case a provider
  // reports them outside the prompt count.
  const cacheReadShare = share(cacheReadTokens, promptTokens);
  const remainingToday = quota?.remaining_today_usd ?? 0;
  const dailyLimit = quota?.daily_limit_usd;

  const values = {
    cost: formatUsd(stats?.usage.cost_usd ?? 0),
    requests: fmt.number(stats?.usage.requests ?? 0),
    input: fmt.number(promptTokens),
    output: fmt.number(stats?.usage.completion_tokens ?? 0),
  };
  // Beyond "999,999,999" the whole summary row steps down in size together.
  const compact = Object.values(values).some((value) => value.length > 11);

  const costLabel = t('dashboard.usage.cost', 'Estimated cost');
  const requestsLabel = t('dashboard.usage.requests', 'Requests');
  const inputLabel = t('dashboard.usage.input_tokens', 'Input Tokens');
  const outputLabel = t('dashboard.usage.output_tokens', 'Output Tokens');

  return (
    <div className="rounded-xl bg-white p-4 shadow-sm ring-1 ring-gray-200 sm:p-6">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h2 className="text-base sm:text-lg font-semibold tracking-tight text-gray-900">
          {t('dashboard.usage.title', 'Usage Statistics')}
        </h2>
        <Select<'today' | 'month' | 'all'>
          value={period}
          onValueChange={setPeriod}
          options={[
            { value: 'today', label: t('dashboard.usage.period_today', 'Today') },
            { value: 'month', label: t('dashboard.usage.period_month', 'This Month') },
            { value: 'all', label: t('dashboard.usage.period_all', 'All Time') },
          ]}
          aria-label={t('dashboard.usage.title', 'Usage Statistics')}
          className="w-32 shrink-0"
        />
      </div>

      {error && (
        <div className="rounded-md bg-red-50 px-4 py-3 text-red-700 ring-1 ring-inset ring-red-200">
          {t(
            'dashboard.usage.load_failed',
            'Failed to load usage statistics. Please try again later.',
          )}
        </div>
      )}

      {isLoading && (
        <div className="flex justify-center py-8">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600"></div>
        </div>
      )}

      {!isLoading && stats && (
        <div className="space-y-4">
          {stats.quota && !stats.quota.has_key && (
            <div className="rounded-md bg-red-50 px-4 py-3 text-red-700 ring-1 ring-inset ring-red-200">
              {t('dashboard.usage.no_key', 'API key not found')}
            </div>
          )}

          {/* Today's quota: independent of the period selected above. */}
          {quota?.has_key && (
            <div className="flex flex-col gap-3 rounded-lg bg-gray-50 px-4 py-3 ring-1 ring-inset ring-gray-200 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
              <div className="min-w-0 sm:flex-1">
                <div className="text-xs font-medium text-gray-500">
                  {t('dashboard.usage.quota_remaining', 'Daily quota remaining')}
                </div>
                <div className="mt-0.5 flex flex-wrap items-baseline gap-x-1.5">
                  <span className="text-xl font-semibold tracking-tight text-gray-900">
                    {formatUsd(remainingToday)}
                  </span>
                  {dailyLimit != null && (
                    <span className="text-sm text-gray-500">
                      {fill(t('dashboard.usage.quota_of', 'of {limit}'), {
                        limit: formatUsd(dailyLimit),
                      })}
                    </span>
                  )}
                </div>
                {dailyLimit != null && dailyLimit > 0 && (
                  <div className="mt-2 max-w-md">
                    <ShareBar value={share(remainingToday, dailyLimit)} />
                  </div>
                )}
              </div>
              <div className="space-y-1 text-xs text-gray-500 sm:max-w-xs sm:text-right">
                <div>
                  {quota.reset_at
                    ? fill(t('dashboard.usage.resets_at', 'Resets at {time}.'), {
                        time: fmt.dateTime(quota.reset_at, RESET_AT_FORMAT),
                      })
                    : t('dashboard.usage.resets_next', 'Resets at the next daily reset.')}
                </div>
                {quota.max_concurrency != null && (
                  <div>
                    {fill(
                      t('dashboard.usage.max_concurrency', 'Max concurrent requests: {limit}'),
                      {
                        limit:
                          quota.max_concurrency === 0
                            ? t('dashboard.usage.unlimited', 'Unlimited')
                            : fmt.number(quota.max_concurrency),
                      },
                    )}
                  </div>
                )}
                {contactEmail && (
                  <div>
                    {t('dashboard.usage.more_quota_lead', 'Need more quota? Email')}{' '}
                    <a
                      className="break-words font-medium text-blue-700 underline-offset-2 hover:underline"
                      href={`mailto:${contactEmail}`}
                    >
                      {contactEmail}
                    </a>{' '}
                    {t('dashboard.usage.more_quota_tail', 'and explain your use case.')}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Four equal summary metrics, with token details in their own row below. */}
          <div
            className={clsx(
              'grid gap-x-6 gap-y-5 py-2 sm:py-3 lg:grid-cols-4',
              compact ? 'grid-cols-1 sm:grid-cols-2' : 'grid-cols-2',
            )}
          >
            <StatMetric
              label={costLabel}
              helpLabel={helpLabel(costLabel)}
              help={t(
                'dashboard.usage.cost_help',
                'Internal cost attributed from logged usage. Today and This Month start at 00:00 UTC, when the daily quota resets. Not a bill — for transparency only.',
              )}
              value={values.cost}
              compact={compact}
            />

            <StatMetric
              label={requestsLabel}
              helpLabel={helpLabel(requestsLabel)}
              help={t(
                'dashboard.usage.requests_help',
                'Number of API requests (including errors). Retries count as separate requests.',
              )}
              value={values.requests}
              compact={compact}
            />

            <StatMetric
              label={inputLabel}
              helpLabel={helpLabel(inputLabel)}
              help={
                <>
                  {t(
                    'dashboard.usage.input_tokens_help',
                    'Tokens in your prompts and tool inputs sent to the model.',
                  )}{' '}
                  {t(
                    'dashboard.usage.cache_help',
                    'Cache reads are input tokens served from the prompt cache, usually billed at a lower rate; cache writes are input tokens stored into it. Both are part of Input Tokens.',
                  )}
                </>
              }
              value={values.input}
              compact={compact}
            />

            <StatMetric
              label={outputLabel}
              helpLabel={helpLabel(outputLabel)}
              help={
                <>
                  {t(
                    'dashboard.usage.output_tokens_help',
                    'Tokens generated by the model in responses.',
                  )}{' '}
                  {t(
                    'dashboard.usage.reasoning_help',
                    'Reasoning tokens are spent by the model thinking before it answers. Depending on the provider, they may already be counted in Output Tokens.',
                  )}
                </>
              }
              value={values.output}
              compact={compact}
            />
          </div>

          <div
            className={clsx(
              'grid grid-cols-2 gap-x-6 gap-y-4 border-t border-gray-200 pt-4',
              cacheWriteTokens > 0 ? 'sm:grid-cols-4' : 'sm:grid-cols-3',
            )}
          >
            <div className="col-span-2 min-w-0">
              <Detail label={t('dashboard.usage.cache_reads_label', 'Cache reads')}>
                {fmt.number(cacheReadTokens)}
                <span className="font-normal text-gray-500">
                  {' · '}
                  {fmt.number(cacheReadShare, { style: 'percent', maximumFractionDigits: 0 })}
                </span>
              </Detail>
              <div className="mt-2 max-w-sm">
                <ShareBar value={cacheReadShare} />
              </div>
            </div>
            {cacheWriteTokens > 0 && (
              <Detail label={t('dashboard.usage.cache_writes_label', 'Cache writes')}>
                {fmt.number(cacheWriteTokens)}
              </Detail>
            )}
            {/* No share or bar: providers differ on whether reasoning is part of output. */}
            <Detail label={t('dashboard.usage.reasoning_label', 'Reasoning tokens')}>
              {fmt.number(reasoningTokens)}
            </Detail>
          </div>

          <p className="text-xs leading-relaxed text-gray-500">
            {t(
              'dashboard.usage.footnote',
              'All prompts and responses are logged and may be open-sourced after anonymization. Free within plan limits; dollar amounts are attribution from logs, not invoices.',
            )}
          </p>
        </div>
      )}
    </div>
  );
}
