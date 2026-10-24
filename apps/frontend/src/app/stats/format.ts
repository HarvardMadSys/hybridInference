import type { ClientKind } from '@/lib/api/publicStats';

export const KIND_ORDER: ClientKind[] = ['coding', 'general', 'custom', 'chat', 'direct'];

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

// Every formatter takes the `Intl` locale to write in; the default is the
// page's original English. `useStatsFormat` binds them to the reader's language.

export function fmtCompact(n: number, locale = 'en-US'): string {
  return Math.abs(n) >= 10_000
    ? new Intl.NumberFormat(locale, { notation: 'compact', maximumFractionDigits: 1 }).format(n)
    : new Intl.NumberFormat(locale).format(Math.round(n));
}

export function fmtWhole(n: number, locale = 'en-US'): string {
  return new Intl.NumberFormat(locale).format(n);
}

export function fmtPercent(share: number): string {
  const pct = share * 100;
  return `${pct >= 10 || pct === 0 ? pct.toFixed(0) : pct >= 1 ? pct.toFixed(1) : pct.toFixed(2)}%`;
}

export function fmtDay(iso: string, locale = 'en-US'): string {
  // Dates in the snapshot are UTC calendar days; render them as such.
  return new Date(`${iso.slice(0, 10)}T00:00:00Z`).toLocaleDateString(locale, {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
}

export function fmtDate(iso: string, locale = 'en-US'): string {
  return new Date(iso).toLocaleDateString(locale, {
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

export function countryName(code: string, alpha2: string | null, locale = 'en'): string {
  return (alpha2 && displayName('region', locale, alpha2)) || code;
}

export function languageName(code: string, locale = 'en'): { name: string; native: string | null } {
  const name = displayName('language', locale, code) ?? code;
  const native = displayName('language', code, code);
  return { name, native: native && native.toLowerCase() !== name.toLowerCase() ? native : null };
}
