'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useBranding } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { useAuthAppearance } from '@/site-ui/appearance';
import { AuthPageFrame } from '@/site-ui/SiteUiBoundary';
import { fill } from '@/lib/utils/interpolate';
import { TermsSections } from '@/site-ui/terms-sections';

// Every consent below folds into the backend's single `accepted_tos` flag, so
// each one is mandatory: there is no column to record a partial answer.
const CONSENT_KEYS = ['age', 'terms', 'research', 'sharing'] as const;
type ConsentKey = (typeof CONSENT_KEYS)[number];
type ConsentState = Record<ConsentKey, boolean>;

const INITIAL_CONSENT: ConsentState = { age: false, terms: false, research: false, sharing: false };

const checkboxClassName =
  'mt-0.5 h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500';

// Tolerance for sub-pixel scroll positions at the end of the terms box.
const SCROLL_END_SLACK_PX = 8;

function isScrolledToEnd(el: HTMLElement): boolean {
  return el.scrollTop + el.clientHeight >= el.scrollHeight - SCROLL_END_SLACK_PX;
}

function ConsentBlock({
  step,
  title,
  children,
}: {
  step: number;
  title: string;
  children: React.ReactNode;
}): JSX.Element {
  const skin = useAuthAppearance();
  return (
    <section
      className={`rounded-lg border px-4 py-4 ${skin.consentBlock}`}
      data-auth="consent-section"
    >
      <h2 className="text-sm font-semibold text-gray-900">
        <span className="mr-2 inline-flex h-5 w-5 items-center justify-center rounded-full bg-gray-900 text-xs font-medium text-white">
          {step}
        </span>
        {title}
      </h2>
      <div className="mt-2 space-y-2 text-sm text-gray-700">{children}</div>
    </section>
  );
}

export function SignupConsentStep({ onContinue }: { onContinue: () => void }): JSX.Element {
  const t = useT();
  const skin = useAuthAppearance();
  const branding = useBranding();
  const [consent, setConsent] = useState<ConsentState>(INITIAL_CONSENT);
  // The Terms checkbox stays disabled until the embedded terms have been
  // scrolled to the end once; scrolling back up does not re-lock it.
  const [termsRead, setTermsRead] = useState(false);
  const termsRef = useRef<HTMLDivElement>(null);
  const allChecked = CONSENT_KEYS.every((key) => consent[key]);

  useEffect(() => {
    // Terms short enough to need no scrolling count as read on first paint.
    const el = termsRef.current;
    if (el && el.clientHeight > 0 && el.scrollHeight <= el.clientHeight) setTermsRead(true);
  }, []);

  const toggle = (key: ConsentKey) => () =>
    setConsent((current) => ({ ...current, [key]: !current[key] }));

  return (
    <AuthPageFrame
      page="signup"
      kicker={t('auth.consent.kicker', 'BEFORE YOU START')}
      title={t('auth.consent.title', 'Before you create an account')}
      subtitle={fill(
        t(
          'auth.consent.intro',
          '{app_name} is an experimental research service. Please read and confirm each item below.',
        ),
        { app_name: branding.appName },
      )}
      // The step renders inside `AuthPageFrame` like the form after it, so the
      // cross-link is the frame's to place: the default card puts it after the
      // confirmations, a module frame wherever its design does. The step draws
      // no copy of its own, or the default card shows two.
      topbar={
        <>
          {t('auth.signup.have_account', 'Already have an account?')}{' '}
          <Link href="/login" prefetch={false}>
            {t('auth.signup.login_link', 'Log In')}
          </Link>
        </>
      }
    >
      <div className={skin.form} data-auth="form">
        <ConsentBlock step={1} title={t('auth.consent.age_title', 'Age requirement')}>
          <p>
            {fill(
              t('auth.consent.age_body', '{app_name} is available only to adults age 18 or older.'),
              { app_name: branding.appName },
            )}
          </p>
          <label className="flex items-start gap-3 font-medium text-gray-900" data-auth="consent">
            <input
              type="checkbox"
              className={checkboxClassName}
              checked={consent.age}
              onChange={toggle('age')}
            />
            <span>
              {t('auth.consent.age_confirm', 'I confirm that I am at least 18 years old.')}
            </span>
          </label>
        </ConsentBlock>

        <ConsentBlock step={2} title={t('auth.consent.terms_title', 'Terms of Service')}>
          <p>
            {t(
              'auth.consent.terms_body',
              'Please read the terms in full. The checkbox unlocks once you reach the end.',
            )}
          </p>
          <div
            ref={termsRef}
            role="region"
            aria-label={t('auth.consent.terms_title', 'Terms of Service')}
            tabIndex={0}
            className={`max-h-56 overflow-y-auto rounded-md border bg-white px-4 py-3 focus:outline-none focus:ring-2 focus:ring-blue-500/40 ${skin.consentBlock}`}
            onScroll={(event) => {
              if (!termsRead && isScrolledToEnd(event.currentTarget)) setTermsRead(true);
            }}
          >
            <TermsSections headingLevel={3} compact />
          </div>
          <p className="text-xs text-gray-500">
            <Link
              className="font-medium text-blue-600 hover:text-blue-700"
              href="/terms"
              target="_blank"
              rel="noopener noreferrer"
            >
              {t('auth.consent.terms_link', 'Open the full Terms of Service in a new tab')}
            </Link>
          </p>
          <label
            className={`flex items-start gap-3 font-medium ${
              termsRead ? 'text-gray-900' : 'text-gray-400'
            }`}
            data-auth="consent"
          >
            <input
              type="checkbox"
              className={checkboxClassName}
              checked={consent.terms}
              disabled={!termsRead}
              onChange={toggle('terms')}
            />
            <span>{t('auth.consent.terms_agree', 'I agree to the Terms of Service.')}</span>
          </label>
          {!termsRead && (
            <p className="text-xs text-gray-500">
              {t(
                'auth.consent.terms_scroll_hint',
                'Scroll to the end of the terms to enable this checkbox.',
              )}
            </p>
          )}
        </ConsentBlock>

        <ConsentBlock step={3} title={t('auth.consent.research_title', 'Research participation')}>
          <p>
            {fill(
              t(
                'auth.consent.research_body',
                '{app_name} is operated to study how people and software agents use large language models. If you participate, we may collect and analyze:',
              ),
              { app_name: branding.appName },
            )}
          </p>
          <ul className="list-disc space-y-0.5 pl-5">
            <li>
              {fill(t('auth.consent.research_item_1', 'prompts sent through {app_name};'), {
                app_name: branding.appName,
              })}
            </li>
            <li>{t('auth.consent.research_item_2', 'model responses;')}</li>
            <li>{t('auth.consent.research_item_3', 'tool calls and tool outputs;')}</li>
            <li>{t('auth.consent.research_item_4', 'model and provider information;')}</li>
            <li>
              {t('auth.consent.research_item_5', 'timestamps and request/session information;')}
            </li>
            <li>
              {t(
                'auth.consent.research_item_6',
                'token counts, latency, routing, and other usage metadata.',
              )}
            </li>
          </ul>
          <p>
            {t(
              'auth.consent.research_use',
              'These data may be used by the research team to characterize LLM workloads, evaluate serving systems, and publish research results.',
            )}
          </p>
          <p className="font-medium">
            {t(
              'auth.consent.research_warning',
              'Do not submit passwords, credentials, confidential information, regulated data, or sensitive personal information.',
            )}
          </p>
          <p>
            {t(
              'auth.consent.research_voluntary',
              'Participation is voluntary. If you do not agree, you cannot use the research service.',
            )}
          </p>
          <label className="flex items-start gap-3 font-medium text-gray-900" data-auth="consent">
            <input
              type="checkbox"
              className={checkboxClassName}
              checked={consent.research}
              onChange={toggle('research')}
            />
            <span>
              {fill(
                t(
                  'auth.consent.research_consent',
                  'I consent to participate in this research and to the collection and analysis of my {app_name} usage data.',
                ),
                { app_name: branding.appName },
              )}
            </span>
          </label>
        </ConsentBlock>

        <ConsentBlock step={4} title={t('auth.consent.sharing_title', 'Research data sharing')}>
          <p>
            {t(
              'auth.consent.sharing_body_1',
              'Some data from this study may be included in research publications or released as a research dataset. Released data may include sanitized prompts and responses, tool calls and outputs, timing information, model and framework information, and usage and performance metadata.',
            )}
          </p>
          <p>
            {t(
              'auth.consent.sharing_body_2',
              'Before public release, we process the data to remove or redact direct identifiers and detected personally identifiable information. Automated sanitization cannot guarantee removal of every sensitive or identifying detail.',
            )}
          </p>
          <p>
            {t(
              'auth.consent.sharing_body_3',
              'Once de-identified data have been publicly released, it may no longer be possible to withdraw or delete those copies.',
            )}
          </p>
          <label className="flex items-start gap-3 font-medium text-gray-900" data-auth="consent">
            <input
              type="checkbox"
              className={checkboxClassName}
              checked={consent.sharing}
              onChange={toggle('sharing')}
            />
            <span>
              {fill(
                t(
                  'auth.consent.sharing_consent',
                  'I understand and consent to the sharing and possible public release of de-identified research data derived from my {app_name} usage.',
                ),
                { app_name: branding.appName },
              )}
            </span>
          </label>
        </ConsentBlock>

        <div className="mt-6 space-y-4">
          <button
            type="button"
            className={skin.submit}
            data-auth="submit"
            disabled={!allChecked}
            onClick={onContinue}
          >
            {t('auth.consent.continue', 'Continue')}
          </button>
          {!allChecked && (
            <p className="text-center text-xs">
              {t('auth.consent.all_required', 'All four confirmations are required to continue.')}
            </p>
          )}
        </div>
      </div>
    </AuthPageFrame>
  );
}
