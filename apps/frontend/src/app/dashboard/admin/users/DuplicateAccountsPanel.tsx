'use client';

import Link from 'next/link';
import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import {
  scanDuplicateAccounts,
  type DuplicateAccountGroup,
  type DuplicateAccountMember,
  type DuplicateAccountScan,
} from '@/lib/api/admin';
import { Select } from '@/components/ui/Select';
import { PILL } from '@/components/features/admin/adminStyles';
import { getErrorMessage } from '@/lib/utils/errors';
import { userAccountLabel } from '@/lib/utils/userLabel';
import { userDetailHref } from './lib/userLink';
import { BUTTON_SECONDARY, FIELD_LABEL, ROLE_STYLE, STATUS_STYLE } from './ui';

const WINDOW_OPTIONS = [
  { value: '1', label: 'Last day' },
  { value: '7', label: 'Last 7 days' },
  { value: '14', label: 'Last 14 days' },
  { value: '30', label: 'Last 30 days' },
  { value: '90', label: 'Last 90 days' },
];

/** What each kind of evidence is called in a group's summary. */
export const SIGNAL_LABELS: Record<string, string> = {
  same_browser: 'Same browser',
  email_alias: 'Same mailbox',
  signup_burst: 'Registered together',
  signup_ip: 'Signup address',
  login_ip: 'Sign-in address',
  cross_ip: 'API from sign-in address',
  co_active: 'Active together',
  request_ip: 'Request address',
  activity_profile: 'Same weekly rhythm',
  same_client: 'Same client',
};

const plural = (count: number, one: string, many = `${one}s`) =>
  `${count.toLocaleString('en-US')} ${count === 1 ? one : many}`;

const day = (iso: string | null) =>
  iso
    ? new Date(iso).toLocaleDateString('en-US', {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      })
    : '—';

function MemberRow({ member }: { member: DuplicateAccountMember }) {
  const status = STATUS_STYLE[member.status];
  return (
    <li className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
      <Link
        href={userDetailHref(member.user_id)}
        prefetch={false}
        className="font-medium text-gray-900 hover:underline"
        title={member.user_name || member.user_id}
      >
        {userAccountLabel(member)}
      </Link>
      <span
        className={`${PILL} ring-1 ring-inset ${status?.pill ?? 'bg-gray-100 text-gray-600 ring-gray-500/20'}`}
      >
        {status?.label ?? member.status}
      </span>
      {member.role !== 'free' && (
        <span className={`${PILL} ${ROLE_STYLE[member.role] ?? ROLE_STYLE.free}`}>
          {member.role}
        </span>
      )}
      <span className="text-[12px] text-gray-500">
        {plural(member.requests, 'request')} · joined {day(member.created_at)}
      </span>
    </li>
  );
}

function GroupCard({ group }: { group: DuplicateAccountGroup }) {
  const byId = new Map(group.members.map((member) => [member.user_id, member]));
  const label = (userId: string) => userAccountLabel(byId.get(userId) ?? { id: userId });
  return (
    <li className="rounded-md border border-gray-200 p-3" data-testid="duplicate-account-group">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-semibold text-gray-900">Score {group.score}</span>
        <span className="text-[12px] text-gray-500">{plural(group.members.length, 'account')}</span>
        {group.signals.map((kind) => (
          <span key={kind} className={`${PILL} bg-gray-100 text-gray-700`}>
            {SIGNAL_LABELS[kind] ?? kind}
          </span>
        ))}
      </div>
      {group.address_only && (
        <p className="mt-1 text-[12px] text-amber-700">
          Shared addresses only: people in one household or office look like this too.
        </p>
      )}
      <ul className="mt-2 space-y-1">
        {group.members.map((member) => (
          <MemberRow key={member.user_id} member={member} />
        ))}
      </ul>
      <details className="mt-2">
        <summary className="cursor-pointer text-[12px] font-medium text-gray-600 hover:text-gray-900">
          Why these accounts ({plural(group.links.length, 'link')})
        </summary>
        <ul className="mt-2 space-y-2">
          {group.links.map((link) => (
            <li key={link.user_ids.join(' ')}>
              <p className="text-[12px] font-medium text-gray-800">
                {link.user_ids.map(label).join(' ↔ ')}
                <span className="font-normal text-gray-500"> · weight {link.weight}</span>
              </p>
              <ul className="ml-5 list-disc text-[12px] text-gray-600">
                {link.evidence.map((item) => (
                  <li key={item.kind}>{item.detail}</li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      </details>
    </li>
  );
}

function ScanResult({ result }: { result: DuplicateAccountScan }) {
  const asideNotes = [
    result.shared_addresses > 0 &&
      `${plural(result.shared_addresses, 'address', 'addresses')} shared by more than 5 accounts`,
    result.shared_browsers > 0 &&
      `${plural(result.shared_browsers, 'browser')} holding more than 10 accounts`,
  ].filter(Boolean);
  return (
    <div className="space-y-3">
      <p role="status" className="text-[12px] text-gray-600">
        {result.total_groups === 0
          ? `No accounts look like one person's among the ${plural(result.accounts_scanned, 'account')} active since ${day(result.since)}.`
          : `${plural(result.total_groups, 'group')} among the ${plural(result.accounts_scanned, 'account')} active since ${day(result.since)}, highest score first.`}
        {result.total_groups > result.groups.length &&
          ` Showing the first ${result.groups.length}.`}
        {asideNotes.length > 0 && ` Set aside as shared: ${asideNotes.join(' and ')}.`}
      </p>
      {result.groups.length > 0 && (
        <ul className="space-y-2">
          {result.groups.map((group) => (
            <GroupCard key={group.user_ids.join(' ')} group={group} />
          ))}
        </ul>
      )}
      {result.oversized.length > 0 && (
        <p className="text-[12px] text-gray-500">
          {plural(result.oversized.length, 'larger component')} (
          {result.oversized.map((part) => part.user_ids.length).join(', ')} accounts) linked too
          loosely to be one person&apos;s, most likely through a shared network. Look at the
          addresses before acting on any of them.
        </p>
      )}
    </div>
  );
}

/**
 * The Users tab's duplicate-account scan: groups of accounts that may belong
 * to one person, with the evidence behind each. A scan reads the whole
 * window's request log, so it runs only when the administrator asks.
 */
export function DuplicateAccountsPanel() {
  const [days, setDays] = useState('14');
  const scan = useMutation({ mutationFn: (window: number) => scanDuplicateAccounts(window) });

  return (
    <section aria-label="Duplicate accounts" className="rounded-lg border border-gray-200 bg-white">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
        <span className={FIELD_LABEL}>Duplicate accounts</span>
        <span className="text-[12px] text-gray-500">
          Accounts that may belong to one person: shared addresses, registrations and activity at
          the same time, one browser or mailbox.
        </span>
        <div className="ml-auto flex items-center gap-2">
          <Select
            size="sm"
            className="w-32"
            value={days}
            onValueChange={setDays}
            options={WINDOW_OPTIONS}
            aria-label="Scan window"
            disabled={scan.isPending}
          />
          <button
            type="button"
            className={BUTTON_SECONDARY}
            onClick={() => scan.mutate(Number(days))}
            disabled={scan.isPending}
          >
            {scan.isPending ? 'Scanning…' : 'Scan'}
          </button>
          {(scan.data || scan.isError) && !scan.isPending && (
            <button
              type="button"
              className="text-[12px] font-medium text-gray-600 hover:text-gray-900"
              onClick={() => scan.reset()}
            >
              Hide results
            </button>
          )}
        </div>
      </div>
      {(scan.isPending || scan.data || scan.isError) && (
        <div className="border-t border-gray-100 p-3">
          {scan.isPending && (
            <p role="status" className="text-[12px] text-gray-500">
              Scanning the last {plural(Number(days), 'day')}…
            </p>
          )}
          {scan.isError && (
            <p role="alert" className="text-[12px] text-red-600">
              {getErrorMessage(scan.error)}
            </p>
          )}
          {scan.data && !scan.isPending && <ScanResult result={scan.data} />}
        </div>
      )}
    </section>
  );
}
