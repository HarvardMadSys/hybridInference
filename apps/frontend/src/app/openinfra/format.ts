'use client';

import { useMemo } from 'react';

import { useLocale } from '@/components/providers/LocaleProvider';
import { useT } from '@/components/providers/useT';
import { fill } from '@/lib/utils/interpolate';

/** The dash a missing value is written as. */
export const NONE = '—';

function isNumber(value: number | null | undefined): value is number {
  return typeof value === 'number' && !Number.isNaN(value);
}

/**
 * The page's numbers and times in the reader's language: whole and decimal
 * counts, percents, compact counts ("1.2M" / "120万"), seconds, and how long
 * ago something happened.
 */
export function useInfraFormat() {
  const locale = useLocale();
  const t = useT();
  const intl = locale === 'zh-CN' ? 'zh-CN' : 'en-US';
  return useMemo(() => {
    const digits = [0, 1, 2].map((d) => new Intl.NumberFormat(intl, { maximumFractionDigits: d }));
    const twoPlaces = new Intl.NumberFormat(intl, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    const compactFormat = new Intl.NumberFormat(intl, {
      notation: 'compact',
      maximumFractionDigits: 1,
    });

    /** A number with at most `places` decimals. */
    const n = (value: number | null | undefined, places: 0 | 1 | 2 = 0): string =>
      isNumber(value) ? digits[places].format(value) : NONE;
    /** A value already in percent. */
    const pct = (value: number | null | undefined): string =>
      isNumber(value) ? `${digits[1].format(value)}%` : NONE;
    /** A count, shortened from a thousand up. */
    const compact = (value: number | null | undefined): string => {
      if (!isNumber(value)) return NONE;
      return Math.abs(value) >= 1000 ? compactFormat.format(value) : digits[0].format(value);
    };
    /** Seconds, to the hundredth. */
    const sec = (value: number | null | undefined): string =>
      isNumber(value) ? `${twoPlaces.format(value)}s` : NONE;
    /** Seconds written as milliseconds, to the hundredth. */
    const ms = (seconds: number | null | undefined): string =>
      isNumber(seconds) ? `${digits[2].format(seconds * 1000)} ms` : NONE;
    /** Megabytes written as gigabytes. */
    const gb = (mb: number | null | undefined, places: 0 | 1 = 0): string =>
      digits[places].format((mb || 0) / 1024);

    const ago = (ts: number | null | undefined, now: number): string => {
      if (!ts) return t('openinfra.ago.never', 'never');
      const s = Math.max(0, Math.round((now - ts) / 1000));
      if (s < 5) return t('openinfra.ago.just_now', 'just now');
      if (s < 60) return fill(t('openinfra.ago.seconds', '{count}s ago'), { count: String(s) });
      if (s < 3600) {
        return fill(t('openinfra.ago.minutes', '{count}m ago'), {
          count: String(Math.round(s / 60)),
        });
      }
      return fill(t('openinfra.ago.hours', '{count}h ago'), {
        count: String(Math.round(s / 3600)),
      });
    };

    /** A wall-clock time, to the second. */
    const clock = (ts: number | null | undefined): string => {
      if (!ts) return NONE;
      const d = new Date(ts);
      if (Number.isNaN(d.getTime())) return NONE;
      return d.toLocaleTimeString(intl, {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      });
    };

    return { t, intl, n, pct, compact, sec, ms, gb, ago, clock };
  }, [intl, t]);
}

export type InfraFormat = ReturnType<typeof useInfraFormat>;
