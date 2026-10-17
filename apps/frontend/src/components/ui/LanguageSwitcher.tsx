'use client';

import { useState } from 'react';

import { useLocale } from '@/components/providers/LocaleProvider';
import { useChooseLocale } from '@/components/providers/localeChoice';
import { useT } from '@/components/providers/useT';
import { LOCALE_NAMES, SUPPORTED_LOCALES, type Locale } from '@/lib/i18n/locales';

function GlobeIcon() {
  return (
    <svg
      aria-hidden="true"
      className="h-4 w-4"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={1.75}
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 21a9 9 0 100-18 9 9 0 000 18zm0 0c2.5-2.4 3.75-5.4 3.75-9S14.5 5.4 12 3m0 18c-2.5-2.4-3.75-5.4-3.75-9S9.5 5.4 12 3M3.6 9h16.8M3.6 15h16.8"
      />
    </svg>
  );
}

/**
 * The header's language control: one button per language the page is not in,
 * each labelled with that language's own name.
 *
 * With two languages that is a single "中文" or "English" — the name a reader
 * of that language recognises, whichever language they are looking at now.
 */
export function LanguageSwitcher() {
  const locale = useLocale();
  const choose = useChooseLocale();
  const t = useT();
  const [busy, setBusy] = useState(false);

  const select = async (next: Locale) => {
    setBusy(true);
    try {
      await choose(next);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {SUPPORTED_LOCALES.filter((other) => other !== locale).map((other) => (
        <button
          key={other}
          type="button"
          lang={other}
          disabled={busy}
          onClick={() => void select(other)}
          title={t('chrome.language.switch_title', 'Change the interface language')}
          className="inline-flex items-center gap-1 rounded-md px-2 py-1.5 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900 disabled:opacity-50 sm:px-3"
        >
          <GlobeIcon />
          {LOCALE_NAMES[other]}
        </button>
      ))}
    </>
  );
}

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
