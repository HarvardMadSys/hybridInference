import type { ClientKind } from '@/lib/api/publicStats';

export const KIND_ORDER: ClientKind[] = ['coding', 'general', 'custom', 'chat', 'direct'];

export const KIND_LABELS: Record<ClientKind, string> = {
  coding: 'Coding agents',
  general: 'General-purpose agents',
  custom: 'Custom agents & scripts',
  chat: 'Chat apps',
  direct: 'Direct API & SDKs',
};

// Categorical slots checked for color-vision-deficiency separation in this
// order; "direct" is the unidentified remainder, so it gets the neutral.
export const KIND_COLORS: Record<ClientKind, string> = {
  coding: '#2a78d6',
  general: '#eb6834',
  custom: '#1baf7a',
  chat: '#eda100',
  direct: '#a6adb7',
};

export const CONTINENTS: Record<string, string> = {
  EU: 'Europe',
  AS: 'Asia',
  NA: 'North America',
  SA: 'South America',
  AF: 'Africa',
  OC: 'Oceania',
  AN: 'Antarctica',
};

const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });
const whole = new Intl.NumberFormat('en-US');

export function fmtCompact(n: number): string {
  return Math.abs(n) >= 10_000 ? compact.format(n) : whole.format(Math.round(n));
}

export function fmtWhole(n: number): string {
  return whole.format(n);
}

export function fmtPercent(share: number): string {
  const pct = share * 100;
  return `${pct >= 10 || pct === 0 ? pct.toFixed(0) : pct >= 1 ? pct.toFixed(1) : pct.toFixed(2)}%`;
}

export function fmtDay(iso: string): string {
  // Dates in the snapshot are UTC calendar days; render them as such.
  return new Date(`${iso.slice(0, 10)}T00:00:00Z`).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
}

export function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  });
}

function displayName(type: 'region' | 'language', locale: string, code: string): string | null {
  try {
    const name = new Intl.DisplayNames([locale], { type, fallback: 'none' }).of(code);
    return name ?? null;
  } catch {
    return null;
  }
}

export function countryName(code: string, alpha2: string | null): string {
  return (alpha2 && displayName('region', 'en', alpha2)) || code;
}

export function languageName(code: string): { name: string; native: string | null } {
  const name = displayName('language', 'en', code) ?? code;
  const native = displayName('language', code, code);
  return { name, native: native && native.toLowerCase() !== name.toLowerCase() ? native : null };
}

export function weekLabel(
  week: string,
  weeks: string[],
  firstPartial: boolean,
  lastPartial: boolean,
): string {
  const partial =
    (firstPartial && week === weeks[0]) || (lastPartial && week === weeks[weeks.length - 1]);
  return `Week of ${fmtDay(week)}${partial ? ' · partial' : ''}`;
}
