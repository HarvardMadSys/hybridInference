'use client';

import { useEffect, useState } from 'react';

/**
 * When a cached admin view's numbers were computed.
 *
 * The Providers tab's percentile and TTFT-scatter views are computed on the
 * server at most every few minutes and kept, so what is on screen can be a few
 * minutes old. This says how old, with the exact UTC time on hover, and keeps
 * saying it while the page stays open: the age is re-read every 30 seconds.
 * While the server computes new numbers (`refreshing`) it says so too.
 */
export function formatComputedAgo(iso: string, now: number = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`;
}

const TICK_MS = 30_000;

export function ComputedAt({
  at,
  refreshing = false,
}: {
  at: string | null | undefined;
  refreshing?: boolean;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(timer);
  }, [at]);

  if (!at) {
    return refreshing ? <span className="text-[11px] text-gray-400">Computing…</span> : null;
  }
  const utc = `${new Date(at).toISOString().replace('T', ' ').slice(0, 19)} UTC`;
  return (
    <span className="text-[11px] text-gray-400" title={`Computed at ${utc}`}>
      Computed {formatComputedAgo(at, now)}
      {refreshing ? ' · refreshing…' : ''}
    </span>
  );
}
