import { describe, expect, it } from 'vitest';
import type { AuditEntry } from '@/lib/api/organization';
import type { Formatter } from '@/lib/i18n/useFormatter';
import {
  auditActor,
  auditDetails,
  auditEvent,
  auditTarget,
  changePercent,
  compactNumber,
  limitUse,
  memberName,
  memberStatus,
  periodLabel,
  periodRange,
  previousPeriodLabel,
  relativeTime,
  requestStatus,
} from './text';

const t = (_slot: string, english: string) => english;
const fmt: Formatter = {
  dateTime: (value, options) => new Date(value).toLocaleString('en-US', options),
  date: (value, options) => new Date(value).toLocaleDateString('en-US', options),
  number: (value, options) => value.toLocaleString('en-US', options),
};

function entry(
  action: string,
  details: Record<string, unknown> | null,
  more: Partial<AuditEntry> = {},
) {
  return {
    id: 1,
    timestamp: '2026-11-18T10:00:00Z',
    action,
    category: 'membership',
    actor: 'member',
    details,
    ...more,
  } as AuditEntry;
}

describe('periods', () => {
  const now = new Date('2026-11-17T23:30:00Z');

  it('covers the last days in UTC, with today', () => {
    expect(periodRange('7d', fmt, now)).toBe('Nov 11 – Nov 17');
    expect(periodRange('30d', fmt, now)).toBe('Oct 19 – Nov 17');
    expect(periodRange('1d', fmt, now)).toBe('Nov 17');
  });

  it('counts the day in UTC, whatever the reader sees on a clock', () => {
    // Late evening on the 17th in New York is already the 18th in UTC.
    expect(periodRange('1d', fmt, new Date('2026-11-18T03:00:00Z'))).toBe('Nov 18');
  });

  it('names a period and the one before it', () => {
    expect(periodLabel('1d', t)).toBe('Today (UTC)');
    expect(periodLabel('30d', t)).toBe('Last 30 days');
    expect(previousPeriodLabel('1d', t)).toBe('vs the day before');
    expect(previousPeriodLabel('7d', t)).toBe('vs previous 7 days');
  });
});

describe('figures', () => {
  it('shortens large counts and leaves small ones whole', () => {
    expect(compactNumber(9_999, fmt)).toBe('9,999');
    expect(compactNumber(808_100, fmt)).toBe('808.1K');
    expect(compactNumber(18_040_000, fmt)).toBe('18M');
  });

  it('says how long ago to the largest unit that fits', () => {
    const now = new Date('2026-11-18T12:00:00Z').getTime();
    expect(relativeTime(null, t, fmt, now)).toBe('Never');
    expect(relativeTime('2026-11-18T11:59:40Z', t, fmt, now)).toBe('Just now');
    expect(relativeTime('2026-11-18T11:33:00Z', t, fmt, now)).toBe('27 min ago');
    expect(relativeTime('2026-11-18T04:00:00Z', t, fmt, now)).toBe('8 h ago');
    expect(relativeTime('2026-11-09T12:00:00Z', t, fmt, now)).toBe('9 d ago');
    expect(relativeTime('2026-08-01T12:00:00Z', t, fmt, now)).toMatch(/Aug 1, 2026/);
  });

  it('measures a month against its limit', () => {
    expect(limitUse(76.7, 130)).toEqual({ percent: 59, tone: 'ok' });
    expect(limitUse(72, 80)).toEqual({ percent: 90, tone: 'near' });
    expect(limitUse(40.6, 35)).toEqual({ percent: 116, tone: 'over' });
    // The tone is of the ratio itself, as the gateway compares it, and the
    // figure is rounded down: neither claims a mark that is not reached.
    expect(limitUse(79.6, 100)).toEqual({ percent: 79, tone: 'ok' });
    expect(limitUse(80, 100)).toEqual({ percent: 80, tone: 'near' });
    expect(limitUse(99.6, 100)).toEqual({ percent: 99, tone: 'near' });
    expect(limitUse(100, 100)).toEqual({ percent: 100, tone: 'over' });
    // 0.29 * 100 is 28.999… in binary: still 29%.
    expect(limitUse(0.29, 1)).toEqual({ percent: 29, tone: 'ok' });
    expect(limitUse(0, 50)).toEqual({ percent: 0, tone: 'ok' });
    // A limit of zero lets nothing through.
    expect(limitUse(0, 0)).toEqual({ percent: 100, tone: 'over' });
    expect(limitUse(12, null)).toBeNull();
  });

  it('compares with the period before only when there is one', () => {
    expect(changePercent(94, 100)).toBe(-6);
    expect(changePercent(150, 100)).toBe(50);
    expect(changePercent(10, 0)).toBeNull();
    expect(changePercent(10, null)).toBeNull();
    expect(changePercent(10, undefined)).toBeNull();
  });
});

describe('members and requests', () => {
  it('names a member by what it has', () => {
    expect(memberName({ user_id: 'u-1', user_name: 'Lei Wang', email: 'lei@example.edu' })).toBe(
      'Lei Wang',
    );
    expect(memberName({ user_id: 'u-1', user_name: ' ', email: 'lei@example.edu' })).toBe(
      'lei@example.edu',
    );
    expect(memberName({ user_id: 'u-1' })).toBe('u-1');
  });

  it('says Removed only for a suspension that is the organization’s own', () => {
    expect(memberStatus({ status: 'active', removed: false }, t).label).toBe('Active');
    expect(memberStatus({ status: 'suspended', removed: true }, t).label).toBe('Removed');
    expect(memberStatus({ status: 'suspended', removed: false }, t).label).toBe('Suspended');
    expect(memberStatus({ status: 'pending_approval', removed: false }, t).label).toBe(
      'Pending approval',
    );
    // A status this console does not know shows as it is.
    expect(memberStatus({ status: 'frozen', removed: false }, t).label).toBe('frozen');
  });

  it('says how a request ended', () => {
    expect(requestStatus({ status_code: 200 }, t).label).toBe('OK');
    expect(requestStatus({ status_code: 429 }, t).label).toBe('429 Rate limited');
    expect(requestStatus({ status_code: 502 }, t).label).toBe('502 Upstream error');
    expect(requestStatus({ status_code: 403 }, t).label).toBe('403 Refused');
    expect(requestStatus({ status_code: null }, t).label).toBe('—');
  });
});

describe('audit rows', () => {
  it('names each action, and shows one it does not know as it is', () => {
    expect(auditEvent('organization.member.join', t)).toBe('Member joined');
    expect(auditEvent('organization.member_limit.set', t)).toBe('Member monthly limit changed');
    expect(auditEvent('organization.admin.add', t)).toBe('Organization administrator added');
    expect(auditEvent('organization.connection.detach', t)).toBe('SSO connection detached');
    expect(auditEvent('organization.something.new', t)).toBe('organization.something.new');
  });

  it('names a platform administrator without its account', () => {
    expect(
      auditActor(entry('organization.create', null, { actor: 'platform_administrator' }), t),
    ).toBe('Platform administrator');
    expect(
      auditActor(entry('organization.member.remove', null, { actor_email: 'yu@example.edu' }), t),
    ).toBe('yu@example.edu');
    expect(auditActor(entry('organization.member.join', null, { actor: 'system' }), t)).toBe(
      'System',
    );
  });

  it('reads a limit in the currency it was recorded in', () => {
    const usd = entry('organization.member_limit.set', {
      old: '20.0000',
      new: '80.0000',
      currency: 'USD',
    });
    const cleared = entry('organization.default_member_limit.set', {
      old: '40',
      new: null,
      currency: 'USD',
    });
    const cny = entry('organization.limit.set', { old: null, new: '7000', currency: 'CNY' });

    expect(auditDetails(usd, t, 'en-US')).toBe('$20.00 to $80.00');
    expect(auditDetails(cleared, t, 'en-US')).toBe('$40.00 to No limit');
    expect(auditDetails(cny, t, 'en-US')).toBe('No limit to ¥7,000.00');
  });

  it('says what the other actions changed', () => {
    expect(
      auditDetails(entry('organization.member.join', { sso_provider: 'example-univ' }), t, 'en-US'),
    ).toBe('Signed in through example-univ');
    expect(
      auditDetails(entry('organization.member.remove', { email: 'a@example.edu' }), t, 'en-US'),
    ).toBe('Account suspended; sessions ended, API keys revoked');
    expect(
      auditDetails(
        entry('organization.admin.add', { role: { old: 'member', new: 'admin' } }),
        t,
        'en-US',
      ),
    ).toBe('Role: Member to Administrator');
    expect(
      auditDetails(
        entry('organization.update', { name: { old: 'Example', new: 'Example University' } }),
        t,
        'en-US',
      ),
    ).toBe('Example to Example University');
    expect(
      auditDetails(
        entry('organization.connection.attach', { provider: 'example-univ', members_added: 3 }),
        t,
        'en-US',
      ),
    ).toBe('example-univ; 3 accounts became members');
    expect(
      auditDetails(
        entry('organization.create', { name: 'Example', providers: ['a', 'b'], members_added: 0 }),
        t,
        'en-US',
      ),
    ).toBe('a, b; 0 accounts became members');
    expect(
      auditDetails(
        entry('organization.connection.attach', { provider: 'example-univ', members_added: 1 }),
        t,
        'en-US',
      ),
    ).toBe('example-univ; 1 account became a member');
    // A role this console does not know shows as it is.
    expect(
      auditDetails(
        entry('organization.admin.remove', { role: { old: 'owner', new: 'member' } }),
        t,
        'en-US',
      ),
    ).toBe('Role: owner to Member');
    expect(auditDetails(entry('organization.something.new', { any: 'thing' }), t, 'en-US')).toBe(
      '',
    );
  });

  it('names what a row acted on', () => {
    expect(
      auditTarget(
        entry('organization.limit.set', null, { target_type: 'organization' }),
        'Example University',
      ),
    ).toBe('Example University');
    expect(
      auditTarget(
        entry('organization.member.remove', null, { target_email: 'mei@example.edu' }),
        'X',
      ),
    ).toBe('mei@example.edu');
    // A member whose account is gone is still named by what the row recorded.
    expect(
      auditTarget(entry('organization.member.remove', { email: 'old@example.edu' }), 'X'),
    ).toBe('old@example.edu');
    expect(
      auditTarget(entry('organization.member.join', null, { target_user_id: 'u-9' }), 'X'),
    ).toBe('u-9');
  });
});
