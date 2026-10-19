'use client';

/**
 * When a cached admin view's numbers were computed.
 *
 * The Providers tab's percentile and TTFT-scatter views are computed on the
 * server at most every few minutes and kept, so what is on screen can be a few
 * minutes old. This says how old, with the exact UTC time on hover.
 */
export function formatComputedAgo(iso: string, now: number = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`;
}

export function ComputedAt({ at }: { at: string | null | undefined }) {
  if (!at) return null;
  const utc = `${new Date(at).toISOString().replace('T', ' ').slice(0, 19)} UTC`;
  return (
    <span className="text-[11px] text-gray-400" title={`Computed at ${utc}`}>
      Computed {formatComputedAgo(at)}
    </span>
  );
}
