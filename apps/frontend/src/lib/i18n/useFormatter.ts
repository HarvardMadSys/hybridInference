'use client';

import { useMemo } from 'react';

import { useLocale } from '@/components/providers/LocaleProvider';
import type { Locale } from './locales';

/** The `Intl` locale each console language formats dates and numbers in. */
const INTL_LOCALE: Readonly<Record<Locale, string>> = {
  en: 'en-US',
  'zh-CN': 'zh-CN',
};

export interface Formatter {
  /** Date and time, e.g. "10/17/2026, 8:00:00 AM" / "2026/10/17 08:00:00". */
  dateTime(value: string | number | Date, options?: Intl.DateTimeFormatOptions): string;
  /** Date only, in the given `Intl` style. */
  date(value: string | number | Date, options?: Intl.DateTimeFormatOptions): string;
  /** A grouped number, e.g. "12,345". */
  number(value: number, options?: Intl.NumberFormatOptions): string;
}

/**
 * Dates and numbers in the console's language, rather than the browser's.
 *
 * A page whose words are Chinese should not print an English month beside
 * them, nor the reverse; this binds every `toLocale*` call to the language the
 * reader chose. Currency stays in US dollars whatever the language.
 */
export function useFormatter(): Formatter {
  const intl = INTL_LOCALE[useLocale()];
  return useMemo(
    () => ({
      dateTime: (value, options) => new Date(value).toLocaleString(intl, options),
      date: (value, options) => new Date(value).toLocaleDateString(intl, options),
      number: (value, options) => value.toLocaleString(intl, options),
    }),
    [intl],
  );
}
