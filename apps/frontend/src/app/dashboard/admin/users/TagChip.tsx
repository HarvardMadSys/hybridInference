'use client';

import type { ReactNode } from 'react';
import type { UserTagSource } from '@/lib/api/admin';

/** The built-in automatic tag: accounts sending requests around the clock. */
export const UNATTENDED_TAG = 'unattended';

const PALETTE = [
  'bg-sky-50 text-sky-700 ring-sky-600/20',
  'bg-violet-50 text-violet-700 ring-violet-600/20',
  'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
  'bg-rose-50 text-rose-700 ring-rose-600/20',
  'bg-teal-50 text-teal-700 ring-teal-600/20',
  'bg-indigo-50 text-indigo-700 ring-indigo-600/20',
];

/** A tag's colors: `unattended` amber, every other tag a stable color of its name. */
export function tagTone(name: string): string {
  if (name === UNATTENDED_TAG) return 'bg-amber-50 text-amber-800 ring-amber-600/30';
  let hash = 0;
  for (const char of name) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return PALETTE[hash % PALETTE.length];
}

const SOURCE_LABEL: Record<UserTagSource, string> = {
  manual: 'added by an administrator',
  auto: 'added automatically',
  dismissed: 'dismissed',
};

/** One tag as a small pill; a dismissed membership is struck through and grey. */
export function TagChip({
  name,
  source,
  title,
  children,
}: {
  name: string;
  source?: UserTagSource;
  title?: string;
  children?: ReactNode;
}) {
  const dismissed = source === 'dismissed';
  const tone = dismissed ? 'bg-gray-50 text-gray-400 ring-gray-300/50' : tagTone(name);
  return (
    <span
      className={`inline-flex max-w-full items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${tone}`}
      title={title ?? (source ? `${name}: ${SOURCE_LABEL[source]}` : name)}
    >
      <span className={`truncate ${dismissed ? 'line-through' : ''}`}>{name}</span>
      {source === 'auto' && (
        <span className="text-[9px] font-semibold uppercase tracking-wide opacity-70">auto</span>
      )}
      {children}
    </span>
  );
}
