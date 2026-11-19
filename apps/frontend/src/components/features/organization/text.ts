import type { Translate } from '@/components/providers/useT';
import type {
  AuditEntry,
  OrganizationMember,
  OrganizationPeriod,
  OrganizationRequest,
} from '@/lib/api/organization';
import type { Formatter } from '@/lib/i18n/useFormatter';
import { fill } from '@/lib/utils/interpolate';
import { formatMoney, type Currency } from '@/lib/utils/money';

/**
 * Words and number formats the organization console's pages share, so a
 * status, a period or an audit event reads the same on every one of them.
 */

export const PERIODS: readonly OrganizationPeriod[] = ['1d', '7d', '30d'];

const PERIOD_DAYS: Readonly<Record<OrganizationPeriod, number>> = { '1d': 1, '7d': 7, '30d': 30 };

export function periodDays(period: OrganizationPeriod): number {
  return PERIOD_DAYS[period];
}

/** "Last 7 days", for a figure that covers the chosen period; one day is today, in UTC. */
export function periodLabel(period: OrganizationPeriod, t: Translate): string {
  if (period === '1d') return t('organization.period.today', 'Today (UTC)');
  return fill(t('organization.period.last_days', 'Last {days} days'), {
    days: String(periodDays(period)),
  });
}

/** "vs previous 7 days", beside a change against the period before. */
export function previousPeriodLabel(period: OrganizationPeriod, t: Translate): string {
  if (period === '1d') return t('organization.period.vs_previous_day', 'vs the day before');
  return fill(t('organization.period.vs_previous_days', 'vs previous {days} days'), {
    days: String(periodDays(period)),
  });
}

/**
 * The UTC days a period covers, as the gateway counts them: the last 1, 7 or
 * 30 days with today. "Nov 11 – Nov 17", or one date for a single day.
 */
export function periodRange(period: OrganizationPeriod, fmt: Formatter, now = new Date()): string {
  const day = (date: Date) => fmt.date(date, { month: 'short', day: 'numeric', timeZone: 'UTC' });
  const last = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  const days = periodDays(period);
  if (days === 1) return day(last);
  const first = new Date(last.getTime() - (days - 1) * 24 * 60 * 60 * 1000);
  return `${day(first)} – ${day(last)}`;
}

/** A member by the name a person reads: its name, else its email, else its id. */
export function memberName(member: {
  user_name?: string | null;
  email?: string | null;
  user_id: string;
}): string {
  return member.user_name?.trim() || member.email || member.user_id;
}

/** Large counts in a few characters: 18.0M tokens, 808.1K. */
export function compactNumber(value: number, fmt: Formatter): string {
  if (Math.abs(value) < 10_000) return fmt.number(value);
  return fmt.number(value, { notation: 'compact', maximumFractionDigits: 1 });
}

/** How long ago something happened, to the largest unit that fits; a date after a month. */
export function relativeTime(
  iso: string | null | undefined,
  t: Translate,
  fmt: Formatter,
  now = Date.now(),
): string {
  if (!iso) return t('organization.time.never', 'Never');
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '—';
  const minutes = Math.floor((now - then) / 60_000);
  if (minutes < 1) return t('organization.time.just_now', 'Just now');
  if (minutes < 60) {
    return fill(t('organization.time.minutes_ago', '{n} min ago'), { n: String(minutes) });
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return fill(t('organization.time.hours_ago', '{n} h ago'), { n: String(hours) });
  }
  const days = Math.floor(hours / 24);
  if (days < 31) {
    return fill(t('organization.time.days_ago', '{n} d ago'), { n: String(days) });
  }
  return fmt.date(iso, { year: 'numeric', month: 'short', day: 'numeric' });
}

/** The tone of a status pill: the pill's colors and its dot's. */
export interface StatusTone {
  label: string;
  pill: string;
  dot: string;
}

const TONE = {
  ok: { pill: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20', dot: 'bg-emerald-500' },
  waiting: { pill: 'bg-indigo-50 text-indigo-700 ring-indigo-600/20', dot: 'bg-indigo-500' },
  held: { pill: 'bg-amber-50 text-amber-700 ring-amber-600/20', dot: 'bg-amber-500' },
  gone: { pill: 'bg-gray-100 text-gray-500 ring-gray-500/20', dot: 'bg-gray-400' },
  bad: { pill: 'bg-red-50 text-red-700 ring-red-600/20', dot: 'bg-red-500' },
} as const;

/**
 * A member's status as its organization reads it. The status is the
 * account's; a member the organization removed says so, because only that
 * suspension is the organization's to undo.
 */
export function memberStatus(
  member: Pick<OrganizationMember, 'status' | 'removed'>,
  t: Translate,
): StatusTone {
  if (member.removed) {
    return { label: t('organization.status.removed', 'Removed'), ...TONE.gone };
  }
  switch (member.status) {
    case 'active':
      return { label: t('organization.status.active', 'Active'), ...TONE.ok };
    case 'pending_approval':
      return { label: t('organization.status.pending', 'Pending approval'), ...TONE.waiting };
    case 'suspended':
      return { label: t('organization.status.suspended', 'Suspended'), ...TONE.held };
    case 'rejected':
      return { label: t('organization.status.rejected', 'Rejected'), ...TONE.bad };
    case 'deleted':
      return { label: t('organization.status.deleted', 'Deleted'), ...TONE.gone };
    default:
      return { label: member.status, ...TONE.gone };
  }
}

/** A request's outcome: OK, or the status code with what it usually means. */
export function requestStatus(
  request: Pick<OrganizationRequest, 'status_code'>,
  t: Translate,
): StatusTone {
  const code = request.status_code;
  if (code == null) return { label: '—', ...TONE.gone };
  if (code >= 200 && code < 400) {
    return { label: t('organization.request.ok', 'OK'), ...TONE.ok };
  }
  if (code === 429) {
    return {
      label: fill(t('organization.request.rate_limited', '{code} Rate limited'), {
        code: String(code),
      }),
      ...TONE.bad,
    };
  }
  if (code >= 500) {
    return {
      label: fill(t('organization.request.upstream_error', '{code} Upstream error'), {
        code: String(code),
      }),
      ...TONE.bad,
    };
  }
  return {
    label: fill(t('organization.request.refused', '{code} Refused'), { code: String(code) }),
    ...TONE.bad,
  };
}

/** How far a month's spend is into its limit: the share, and how to color it. */
export function limitUse(
  spent: number | null,
  limit: number | null,
): { percent: number; tone: 'ok' | 'near' | 'over' } | null {
  if (limit == null || spent == null) return null;
  if (limit <= 0) return { percent: 100, tone: 'over' };
  const percent = Math.round((spent / limit) * 100);
  return { percent, tone: percent >= 100 ? 'over' : percent >= 90 ? 'near' : 'ok' };
}

/** The change of a figure against the period before, as a signed percentage; null without a base. */
export function changePercent(current: number, previous: number | null | undefined): number | null {
  if (previous == null || previous <= 0) return null;
  return Math.round(((current - previous) / previous) * 100);
}

/** What an audit row did, in a few words. An action this console does not know shows as it is. */
export function auditEvent(action: string, t: Translate): string {
  switch (action) {
    case 'organization.member.join':
      return t('organization.audit.event.member_join', 'Member joined');
    case 'organization.member.remove':
      return t('organization.audit.event.member_remove', 'Member removed');
    case 'organization.member.restore':
      return t('organization.audit.event.member_restore', 'Member restored');
    case 'organization.admin.add':
      return t('organization.audit.event.admin_add', 'Organization administrator added');
    case 'organization.admin.remove':
      return t('organization.audit.event.admin_remove', 'Organization administrator removed');
    case 'organization.member_limit.set':
      return t('organization.audit.event.member_limit', 'Member monthly limit changed');
    case 'organization.default_member_limit.set':
      return t('organization.audit.event.default_limit', 'Default monthly limit changed');
    case 'organization.limit.set':
      return t('organization.audit.event.limit', 'Organization monthly limit changed');
    case 'organization.create':
      return t('organization.audit.event.create', 'Organization created');
    case 'organization.update':
      return t('organization.audit.event.update', 'Organization renamed');
    case 'organization.connection.attach':
      return t('organization.audit.event.attach', 'SSO connection attached');
    case 'organization.connection.detach':
      return t('organization.audit.event.detach', 'SSO connection detached');
    default:
      return action;
  }
}

/** Who did it: a member by its email, the platform administrator, or the system. */
export function auditActor(entry: AuditEntry, t: Translate): string {
  if (entry.actor === 'platform_administrator') {
    return t('organization.audit.actor.platform', 'Platform administrator');
  }
  if (entry.actor === 'member') {
    return (
      entry.actor_email || entry.actor_user_id || t('organization.audit.actor.member', 'Member')
    );
  }
  return t('organization.audit.actor.system', 'System');
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

function pair(value: unknown): { old: string | null; next: string | null } | null {
  if (!value || typeof value !== 'object') return null;
  const record = value as Record<string, unknown>;
  return { old: text(record.old), next: text(record.new) };
}

/** A role in the organization, in words; one this console does not know shows as it is. */
function roleName(role: string, t: Translate): string {
  if (role === 'admin') return t('organization.role.admin', 'Administrator');
  if (role === 'member') return t('organization.role.member', 'Member');
  return role;
}

/** A limit an audit row recorded, in the currency it was recorded in. */
function recordedLimit(value: unknown, currency: Currency, locale: string, t: Translate): string {
  const amount = text(value) === null ? null : Number(value);
  if (amount === null || !Number.isFinite(amount)) {
    return t('organization.limit.none', 'No limit');
  }
  return formatMoney(amount, currency, locale, { maximumFractionDigits: 2 });
}

/** What an audit row changed, read from its details; empty when there is nothing to add. */
export function auditDetails(entry: AuditEntry, t: Translate, locale: string): string {
  const details = entry.details ?? {};
  switch (entry.action) {
    case 'organization.limit.set':
    case 'organization.default_member_limit.set':
    case 'organization.member_limit.set': {
      const currency: Currency = details.currency === 'CNY' ? 'CNY' : 'USD';
      return fill(t('organization.audit.details.change', '{old} to {new}'), {
        old: recordedLimit(details.old, currency, locale, t),
        new: recordedLimit(details.new, currency, locale, t),
      });
    }
    case 'organization.member.join': {
      const provider = text(details.sso_provider);
      return provider
        ? fill(t('organization.audit.details.join', 'Signed in through {provider}'), { provider })
        : '';
    }
    case 'organization.member.remove':
      return t(
        'organization.audit.details.remove',
        'Account suspended; sessions ended, API keys revoked',
      );
    case 'organization.member.restore':
      return t('organization.audit.details.restore', 'Account active again');
    case 'organization.admin.add':
    case 'organization.admin.remove': {
      const role = pair(details.role);
      return role?.old && role.next
        ? fill(t('organization.audit.details.role', 'Role: {old} to {new}'), {
            old: roleName(role.old, t),
            new: roleName(role.next, t),
          })
        : '';
    }
    case 'organization.update': {
      const name = pair(details.name);
      return name?.old && name.next
        ? fill(t('organization.audit.details.change', '{old} to {new}'), {
            old: name.old,
            new: name.next,
          })
        : '';
    }
    case 'organization.create':
    case 'organization.connection.attach': {
      const provider =
        text(details.provider) ??
        (Array.isArray(details.providers) ? details.providers.filter(text).join(', ') : null);
      const added = typeof details.members_added === 'number' ? details.members_added : null;
      if (provider && added !== null) {
        return fill(
          added === 1
            ? t(
                'organization.audit.details.attach_one',
                '{provider}; {count} account became a member',
              )
            : t('organization.audit.details.attach', '{provider}; {count} accounts became members'),
          { provider, count: String(added) },
        );
      }
      return provider ?? '';
    }
    case 'organization.connection.detach':
      return text(details.provider) ?? '';
    default:
      return '';
  }
}

/** What an audit row acted on: a member by its email, or the organization. */
export function auditTarget(entry: AuditEntry, organizationName: string): string {
  if (entry.target_type === 'organization') return organizationName;
  const email = entry.details ? text(entry.details.email) : null;
  return entry.target_email || email || entry.target_user_id || '—';
}
