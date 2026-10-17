'use client';

import { useMemo } from 'react';

import { useLocale } from '@/components/providers/LocaleProvider';
import { useT, type Translate } from '@/components/providers/useT';
import type { ClientKind } from '@/lib/api/publicStats';
import { fill } from '@/lib/utils/interpolate';
import {
  countryName,
  fmtCompact,
  fmtDate,
  fmtDay,
  fmtPercent,
  fmtWhole,
  languageName,
} from './format';

function kindLabel(kind: ClientKind, t: Translate): string {
  switch (kind) {
    case 'coding':
      return t('stats.kind.coding', 'Coding agents');
    case 'general':
      return t('stats.kind.general', 'General-purpose agents');
    case 'custom':
      return t('stats.kind.custom', 'Custom agents & scripts');
    case 'chat':
      return t('stats.kind.chat', 'Chat apps');
    default:
      return t('stats.kind.direct', 'Direct API & SDKs');
  }
}

function continentName(code: string, t: Translate): string {
  switch (code) {
    case 'EU':
      return t('stats.continent.eu', 'Europe');
    case 'AS':
      return t('stats.continent.as', 'Asia');
    case 'NA':
      return t('stats.continent.na', 'North America');
    case 'SA':
      return t('stats.continent.sa', 'South America');
    case 'AF':
      return t('stats.continent.af', 'Africa');
    case 'OC':
      return t('stats.continent.oc', 'Oceania');
    case 'AN':
      return t('stats.continent.an', 'Antarctica');
    default:
      return code;
  }
}

/**
 * The stats page's numbers, dates and names in the reader's language: compact
 * counts ("1.2M" / "120万"), UTC days, country and language names from
 * `Intl.DisplayNames`, and the page's own labels for client kinds and
 * continents.
 */
export function useStatsFormat() {
  const locale = useLocale();
  const t = useT();
  const intl = locale === 'zh-CN' ? 'zh-CN' : 'en-US';
  const names = locale === 'zh-CN' ? 'zh' : 'en';
  return useMemo(() => {
    const day = (iso: string) => fmtDay(iso, intl);
    return {
      t,
      compact: (n: number) => fmtCompact(n, intl),
      whole: (n: number) => fmtWhole(n, intl),
      percent: fmtPercent,
      day,
      date: (iso: string) => fmtDate(iso, intl),
      partialDay: (iso: string) =>
        fill(t('stats.partial_day', '{date} · partial day'), { date: day(iso) }),
      country: (code: string, alpha2: string | null) => countryName(code, alpha2, names),
      language: (code: string) => languageName(code, names),
      week: (week: string, weeks: string[], firstPartial: boolean, lastPartial: boolean) => {
        const partial =
          (firstPartial && week === weeks[0]) || (lastPartial && week === weeks[weeks.length - 1]);
        return fill(
          partial
            ? t('stats.week_of_partial', 'Week of {date} · partial')
            : t('stats.week_of', 'Week of {date}'),
          { date: day(week) },
        );
      },
      kind: (kind: ClientKind) => kindLabel(kind, t),
      continent: (code: string) => continentName(code, t),
    };
  }, [intl, names, t]);
}

export type StatsFormat = ReturnType<typeof useStatsFormat>;
