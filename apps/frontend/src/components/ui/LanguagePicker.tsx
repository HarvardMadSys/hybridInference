'use client';

import { useState } from 'react';

import { useLocale } from '@/components/providers/LocaleProvider';
import { useChooseLocale } from '@/components/providers/localeChoice';
import { useT } from '@/components/providers/useT';
import { LOCALE_NAMES, SUPPORTED_LOCALES, type Locale } from '@/lib/i18n/locales';

/**
 * The settings page's language control: every language, the current one
 * marked, as a radio group.
 */
export function LanguagePicker() {
  const locale = useLocale();
  const choose = useChooseLocale();
  const t = useT();
  const [busy, setBusy] = useState(false);

  const select = async (next: Locale) => {
    if (next === locale) return;
    setBusy(true);
    try {
      await choose(next);
    } finally {
      setBusy(false);
    }
  };

  return (
    <fieldset>
      <legend className="sr-only">{t('chrome.language.label', 'Interface language')}</legend>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {SUPPORTED_LOCALES.map((option) => {
          const checked = option === locale;
          return (
            <label
              key={option}
              lang={option}
              className={`flex cursor-pointer items-center gap-3 rounded-lg border px-4 py-3 text-sm font-medium transition-colors ${
                checked
                  ? 'border-gray-900 bg-gray-50 text-gray-900'
                  : 'border-gray-200 text-gray-700 hover:border-gray-300'
              }`}
            >
              <input
                type="radio"
                name="interface-language"
                value={option}
                checked={checked}
                disabled={busy}
                onChange={() => void select(option)}
                className="h-4 w-4 accent-gray-900"
              />
              {LOCALE_NAMES[option]}
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}
