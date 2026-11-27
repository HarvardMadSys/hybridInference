'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { useAuth } from '@/components/providers';
import { useT, type Translate } from '@/components/providers/useT';
import {
  AdminEmptyState,
  AdminLoading,
  AdminNotice,
  AdminSection,
} from '@/components/features/admin/AdminPrimitives';
import {
  BUTTON_PRIMARY,
  CONTROL,
  FIELD_HINT,
  FIELD_INVALID,
  FORM_LABEL,
  NOTICE_ACTION,
  PANEL,
  TABLE_CELL,
} from '@/components/features/admin/adminStyles';
import type { Organization, OrganizationConnection } from '@/lib/api/organization';
import { useOrganization, useSetDefaultMemberLimit } from '@/lib/hooks';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { useMoney } from '@/lib/hooks/useMoney';
import { useFormatter } from '@/lib/i18n/useFormatter';
import { getErrorMessage } from '@/lib/utils/errors';
import { fill } from '@/lib/utils/interpolate';
import { moneyAmount, moneyCurrency } from '@/lib/utils/money';
import { readLimit } from './limits';
import { StatusPill } from '@/components/features/console-shared/StatusPill';
import { LimitBar } from './parts';
import { memberName, relativeTime, type StatusTone } from './text';

const TERM = 'text-[12px] text-gray-500';
const VALUE = 'text-[13px] text-gray-900';
const FACTS = 'grid grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-[12rem_minmax(0,1fr)]';

const ON = { pill: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20', dot: 'bg-emerald-500' };
const OFF = { pill: 'bg-gray-100 text-gray-500 ring-gray-500/20', dot: 'bg-gray-400' };
const MISSING = { pill: 'bg-amber-50 text-amber-700 ring-amber-600/20', dot: 'bg-amber-500' };

function connectionState(connection: OrganizationConnection, t: Translate): StatusTone {
  if (!connection.available) {
    return { label: t('organization.settings.sso_unavailable', 'Not available'), ...MISSING };
  }
  return connection.enabled
    ? { label: t('organization.settings.sso_enabled', 'Enabled'), ...ON }
    : { label: t('organization.settings.sso_disabled', 'Disabled'), ...OFF };
}

/** What a member's first sign-in through a connection does. */
function firstSignIn(connection: OrganizationConnection, t: Translate): string {
  if (!connection.auto_create) {
    return t(
      'organization.settings.sso_no_create',
      'Does not create an account. The platform administrator creates the accounts.',
    );
  }
  return connection.require_approval
    ? t(
        'organization.settings.sso_create_approval',
        'Creates an account that waits for the approval of the platform administrator.',
      )
    : t('organization.settings.sso_create', 'Creates an account that works at once.');
}

/**
 * The default monthly limit of a member, which an organization administrator
 * sets: what a member without a limit of its own may spend in a month.
 */
function DefaultLimit({ organization }: { organization: Organization }) {
  const t = useT();
  const money = useMoney();
  const toast = useAccountToast();
  const save = useSetDefaultMemberLimit();
  // Typed in the currency the organization was read in, never relabelled.
  const currency = moneyCurrency(organization);
  const stored = moneyAmount(organization, 'default_member_monthly_limit');
  const initial = stored === null ? '' : String(stored);
  const [value, setValue] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);
  const fieldId = useId();
  const noteId = `${fieldId}-note`;
  // A saved default comes back with the organization: the field then shows it
  // as the gateway keeps it (a typed 45.50 becomes 45.5), with nothing left to save.
  const shown = useRef(initial);
  useEffect(() => {
    if (shown.current !== initial) {
      shown.current = initial;
      setValue(initial);
    }
  }, [initial]);

  const read = readLimit(value, stored, currency, moneyAmount(organization, 'monthly_limit'));
  // The stored amount, also typed another way (40.0 for 40), is nothing to save.
  const unchanged = 'amount' in read && !read.changed;

  async function submit(): Promise<void> {
    if (submitting.current || unchanged) return;
    if ('problem' in read) {
      setError(
        read.problem === 'invalid'
          ? fill(
              t(
                'organization.limit_dialog.invalid',
                'Enter an amount in {currency}, such as 5 or 12.50, or leave it empty.',
              ),
              { currency },
            )
          : fill(
              t(
                'organization.settings.default_above',
                'The default cannot be above the limit of the organization, {amount}.',
              ),
              { amount: money.value(organization, 'monthly_limit') },
            ),
      );
      return;
    }
    const { amount } = read;
    submitting.current = true;
    setError(null);
    try {
      await save.mutateAsync({ amount, currency });
      toast.success(
        amount === null
          ? t('organization.settings.default_cleared', 'Default monthly limit cleared.')
          : t('organization.settings.default_saved', 'Default monthly limit saved.'),
      );
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      submitting.current = false;
    }
  }

  return (
    <form
      className="max-w-md"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label htmlFor={fieldId} className={FORM_LABEL}>
        {fill(
          t(
            'organization.settings.default_label',
            'Default monthly limit for a member ({currency})',
          ),
          { currency },
        )}
      </label>
      <div className="flex items-center gap-2">
        <input
          id={fieldId}
          value={value}
          onChange={(event) => {
            setValue(event.target.value);
            setError(null);
          }}
          inputMode="decimal"
          autoComplete="off"
          placeholder={t('organization.limit.none', 'No limit')}
          disabled={save.isPending}
          aria-invalid={error ? true : undefined}
          aria-describedby={noteId}
          className={`${CONTROL} w-40 ${error ? FIELD_INVALID : ''}`}
        />
        <button type="submit" className={BUTTON_PRIMARY} disabled={save.isPending || unchanged}>
          {save.isPending ? t('common.loading', 'Loading...') : t('organization.save', 'Save')}
        </button>
      </div>
      {error ? (
        <p id={noteId} role="alert" className="mt-1 text-[12px] text-red-600">
          {error}
        </p>
      ) : (
        <p id={noteId} className={FIELD_HINT}>
          {t(
            'organization.settings.default_hint',
            'Applies at once to each member that has no monthly limit of its own, but not to administrators. Leave empty for no default.',
          )}
        </p>
      )}
    </form>
  );
}

/**
 * The organization as it is set up: its record, its SSO connections and its
 * administrators, which the platform administrator decides, and its limits,
 * of which an organization administrator sets the default for a member.
 */
export function SettingsView() {
  const t = useT();
  const money = useMoney();
  const fmt = useFormatter();
  const { state } = useAuth();
  const { data, error, isLoading, refetch } = useOrganization();

  if (isLoading) return <AdminLoading label={t('common.loading', 'Loading...')} />;
  if (!data) {
    return (
      <AdminNotice
        role="alert"
        action={
          <button type="button" className={NOTICE_ACTION} onClick={() => void refetch()}>
            {t('organization.retry', 'Retry')}
          </button>
        }
      >
        {getErrorMessage(error)}
      </AdminNotice>
    );
  }

  const limit = moneyAmount(data, 'monthly_limit');

  return (
    <div className="space-y-8">
      <AdminSection title={t('organization.settings.organization', 'Organization')}>
        <dl className={`${PANEL} ${FACTS} p-4`}>
          <dt className={TERM}>{t('organization.settings.name', 'Name')}</dt>
          <dd className={VALUE}>{data.name}</dd>
          <dt className={TERM}>{t('organization.settings.id', 'ID')}</dt>
          <dd className={`${VALUE} break-all font-mono text-[12px]`}>{data.id}</dd>
          <dt className={TERM}>{t('organization.settings.created', 'Created')}</dt>
          <dd className={VALUE}>
            {fmt.date(data.created_at, { year: 'numeric', month: 'short', day: 'numeric' })}
          </dd>
          <dt className={TERM}>{t('organization.tabs.members', 'Members')}</dt>
          <dd className={VALUE}>{fmt.number(data.member_count)}</dd>
        </dl>
      </AdminSection>

      <AdminSection
        title={t('organization.settings.sso', 'Single sign-on')}
        description={t(
          'organization.settings.sso_description',
          'An account becomes a member when it has an email in these domains and signs in through the connection. The platform administrator sets the connections up.',
        )}
      >
        {data.connections.length === 0 ? (
          <AdminEmptyState
            title={t(
              'organization.settings.sso_empty',
              'No SSO connection is attached, so no new account becomes a member.',
            )}
          />
        ) : (
          <div className="space-y-3">
            {data.connections.map((connection) => (
              <div key={connection.provider_id} className={`${PANEL} p-4`}>
                <div className="mb-3 flex flex-wrap items-center gap-2">
                  <h3 className="text-[13px] font-semibold text-gray-900">
                    {connection.display_name || connection.provider_id}
                  </h3>
                  <StatusPill tone={connectionState(connection, t)} />
                </div>
                {connection.available ? (
                  <dl className={FACTS}>
                    <dt className={TERM}>{t('organization.settings.sso_id', 'Connection')}</dt>
                    <dd className={`${VALUE} font-mono text-[12px]`}>{connection.provider_id}</dd>
                    <dt className={TERM}>
                      {t('organization.settings.sso_domains', 'Email domains')}
                    </dt>
                    <dd className={`${VALUE} break-words`}>{connection.domains.join(', ')}</dd>
                    <dt className={TERM}>
                      {t('organization.settings.sso_first', 'First sign-in')}
                    </dt>
                    <dd className={VALUE}>{firstSignIn(connection, t)}</dd>
                  </dl>
                ) : (
                  <p className="text-[12px] text-gray-500">
                    {fill(
                      t(
                        'organization.settings.sso_gone',
                        'The deployment has no provider {id} any more, so nobody signs in through this connection. Ask the platform administrator to attach its new id.',
                      ),
                      { id: connection.provider_id },
                    )}
                  </p>
                )}
              </div>
            ))}
          </div>
        )}
      </AdminSection>

      <AdminSection
        title={t('organization.settings.administrators', 'Organization administrators')}
        description={t(
          'organization.settings.administrators_description',
          'The platform administrator gives this role and takes it back.',
        )}
      >
        <div className={`${PANEL} overflow-x-auto`}>
          <table className="min-w-full">
            <tbody>
              {data.administrators.map((administrator) => {
                const name = memberName(administrator);
                return (
                  <tr
                    key={administrator.user_id}
                    className="border-b border-gray-100 last:border-b-0"
                  >
                    <td className={`${TABLE_CELL} whitespace-nowrap font-medium text-gray-900`}>
                      {name}
                      {administrator.user_id === state.user?.id ? (
                        <span className="ml-1.5 rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-gray-600">
                          {t('organization.members.you_badge', 'You')}
                        </span>
                      ) : null}
                    </td>
                    <td className={`${TABLE_CELL} text-gray-500`}>
                      {administrator.email && administrator.email !== name
                        ? administrator.email
                        : ''}
                    </td>
                    <td className={`${TABLE_CELL} whitespace-nowrap text-right text-gray-500`}>
                      {fill(t('organization.settings.last_sign_in', 'Last sign-in {when}'), {
                        when: relativeTime(administrator.last_login_at, t, fmt),
                      })}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </AdminSection>

      <AdminSection title={t('organization.settings.limits', 'Limits')}>
        <div className={`${PANEL} space-y-5 p-4`}>
          <div className="max-w-md">
            <p className={FORM_LABEL}>
              {t('organization.settings.limit', 'Monthly limit of the organization')}
            </p>
            <p className="text-[18px] font-semibold tracking-tight text-gray-900 tabular-nums">
              {limit === null
                ? t('organization.limit.none', 'No limit')
                : money.value(data, 'monthly_limit', { maximumFractionDigits: 2 })}
            </p>
            <p className="mb-1.5 mt-0.5 text-[12px] text-gray-500">
              {fill(
                t(
                  'organization.settings.spent',
                  '{amount} spent this month (UTC). Set by the platform administrator.',
                ),
                { amount: money.value(data, 'spent_month', { maximumFractionDigits: 2 }) },
              )}
            </p>
            <LimitBar spent={moneyAmount(data, 'spent_month')} limit={limit} />
          </div>
          <DefaultLimit organization={data} />
        </div>
      </AdminSection>
    </div>
  );
}
