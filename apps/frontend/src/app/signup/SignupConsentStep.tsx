'use client';

import { useEffect, useId, useRef } from 'react';
import Link from 'next/link';
import { useBranding } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { useAuthAppearance } from '@/site-ui/appearance';
import type { ConsentItems } from '@/site-ui/contract';
import { SITE_UI_CLIENT } from '@/site-ui/module';
import { AuthPageFrame } from '@/site-ui/SiteUiBoundary';
import { fill } from '@/lib/utils/interpolate';
import { TermsSections } from '@/site-ui/terms-sections';
import { CheckIcon, SignupSteps } from './SignupSteps';

/**
 * The step shows one of two sets of legal text and confirmations: the
 * console's terms with the four confirmations below, or — when the compiled-in
 * module publishes its own legal text — the module's `TermsContent` with its
 * `consentItems`. Never a mix: the text a visitor is asked to accept here is
 * the text `/terms` publishes, and `/terms` shows the module's whenever it has
 * one.
 *
 * Either way every confirmation folds into the backend's single `accepted_tos`
 * flag, so each one is mandatory: there is no column to record a partial
 * answer, and this step, not the module, decides that Continue waits for all.
 */
const CONSOLE_CONSENT_IDS = ['age', 'terms', 'research', 'sharing'] as const;

const checkboxClassName =
  'mt-0.5 h-4 w-4 shrink-0 rounded border-gray-300 text-blue-600 focus:ring-blue-500 disabled:cursor-not-allowed';

/**
 * One confirmation's row: a box of its own, so the sentence a visitor agrees
 * to stands apart from the text around it, and the whole row is the target.
 */
function confirmationClassName(checked: boolean, enabled: boolean): string {
  const base = 'flex items-start gap-3 rounded-md border px-3 py-2.5 font-medium transition-colors';
  if (!enabled)
    return `${base} cursor-not-allowed border-dashed border-gray-300 bg-white/60 text-gray-400`;
  return checked
    ? `${base} cursor-pointer border-blue-500 bg-blue-50 text-gray-900`
    : `${base} cursor-pointer border-gray-300 bg-white text-gray-900 hover:border-gray-400`;
}

// Tolerance for sub-pixel scroll positions at the end of the terms box.
const SCROLL_END_SLACK_PX = 8;

function isScrolledToEnd(el: HTMLElement): boolean {
  return el.scrollTop + el.clientHeight >= el.scrollHeight - SCROLL_END_SLACK_PX;
}

/** A checkbox's controlled state, for one confirmation id. */
type Confirm = (id: string) => { checked: boolean; onChange: () => void };

/**
 * What the visitor has done on this step. The page keeps it, so going back
 * from the account form finds every box as it was left.
 */
export interface ConsentProgress {
  accepted: ReadonlySet<string>;
  /** The embedded terms have been scrolled to the end once. */
  termsRead: boolean;
}

export const NO_CONSENT: ConsentProgress = { accepted: new Set(), termsRead: false };

function ConsentBlock({
  step,
  done = false,
  title,
  children,
}: {
  /** The section's number, when the step has more than one. */
  step?: number;
  /** Every confirmation in the section is checked: the number becomes a tick. */
  done?: boolean;
  title: string;
  children: React.ReactNode;
}): JSX.Element {
  const skin = useAuthAppearance();
  return (
    <section
      className={`rounded-lg border px-4 py-4 ${skin.consentBlock}`}
      data-auth="consent-section"
    >
      <h2 className="flex items-center text-sm font-semibold text-gray-900">
        {step === undefined ? null : (
          <span
            className={`mr-2 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs font-medium text-white transition-colors ${
              done ? 'bg-emerald-600' : 'bg-gray-900'
            }`}
          >
            {done ? <CheckIcon /> : step}
          </span>
        )}
        {title}
      </h2>
      <div className="mt-2 space-y-2 text-sm text-gray-700">{children}</div>
    </section>
  );
}

export function SignupConsentStep({
  progress,
  onProgress,
  onContinue,
}: {
  progress: ConsentProgress;
  onProgress: (update: (current: ConsentProgress) => ConsentProgress) => void;
  onContinue: () => void;
}): JSX.Element {
  const t = useT();
  const skin = useAuthAppearance();
  const branding = useBranding();
  const legal = SITE_UI_CLIENT.legalText;
  const required: readonly string[] = legal
    ? legal.consentItems.map((item) => item.id)
    : CONSOLE_CONSENT_IDS;
  const { accepted, termsRead } = progress;
  // The confirmations about the terms stay disabled until the embedded terms
  // have been scrolled to the end once; scrolling back up does not re-lock them.
  const markTermsRead = () => onProgress((current) => ({ ...current, termsRead: true }));
  const termsRef = useRef<HTMLDivElement>(null);
  const checkedCount = required.filter((id) => accepted.has(id)).length;
  const allChecked = checkedCount === required.length;
  // Only the console's own card is known to pad its body by `p-8`, which the
  // sticky bar bleeds into so the text scrolling under it stays hidden.
  const inConsoleCard = !SITE_UI_CLIENT.AuthFrame;

  useEffect(() => {
    // Terms short enough to need no scrolling count as read on first paint.
    const el = termsRef.current;
    if (el && el.clientHeight > 0 && el.scrollHeight <= el.clientHeight) markTermsRead();
    // Once, on mount: `markTermsRead` is a fresh closure every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const confirm: Confirm = (id) => ({
    checked: accepted.has(id),
    onChange: () =>
      onProgress((current) => {
        const next = new Set(current.accepted);
        if (!next.delete(id)) next.add(id);
        return { ...current, accepted: next };
      }),
  });

  // The terms both lists embed: the module's own text when it publishes one,
  // the console's otherwise — the same component `/terms` renders.
  const terms = (
    <div className="relative">
      <div
        ref={termsRef}
        role="region"
        aria-label={t('auth.consent.terms_title', 'Terms of Service')}
        tabIndex={0}
        className={`max-h-64 overflow-y-auto rounded-md border bg-white px-4 py-3 focus:outline-none focus:ring-2 focus:ring-blue-500/40 ${skin.consentBlock}`}
        onScroll={(event) => {
          if (!termsRead && isScrolledToEnd(event.currentTarget)) markTermsRead();
        }}
      >
        {legal ? (
          <legal.TermsContent headingLevel={3} compact />
        ) : (
          <TermsSections headingLevel={3} compact />
        )}
      </div>
      {/* A fade over the last lines says there is more to read. */}
      {!termsRead && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-px bottom-px h-12 rounded-b-md bg-gradient-to-t from-white to-transparent"
        />
      )}
    </div>
  );

  return (
    <AuthPageFrame
      page="signup"
      wide
      kicker={t('auth.consent.kicker', 'BEFORE YOU START')}
      title={t('auth.consent.title', 'Before you create an account')}
      subtitle={fill(
        t(
          'auth.consent.intro',
          legal
            ? 'Please read the terms and confirm each item below.'
            : '{app_name} is an experimental research service. Please read and confirm each item below.',
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
      <SignupSteps current="consent" />
      <div className={skin.form} data-auth="form">
        {legal ? (
          <ModuleConfirmations
            items={legal.consentItems}
            terms={terms}
            termsRead={termsRead}
            confirm={confirm}
          />
        ) : (
          <ConsoleConfirmations terms={terms} termsRead={termsRead} confirm={confirm} />
        )}

        {/* Stays in view while the visitor reads, so how far they are and the
            way on are never a long scroll away. */}
        <div
          className={`sticky bottom-0 z-10 mt-6 space-y-2.5 border-t border-gray-200 bg-white/95 py-3 backdrop-blur ${
            inConsoleCard ? '-mx-8 px-8' : ''
          }`}
        >
          <div className="flex items-center gap-3">
            <div className="flex flex-1 gap-1" aria-hidden="true">
              {required.map((id) => (
                <span
                  key={id}
                  className={`h-1.5 flex-1 rounded-full transition-colors ${
                    accepted.has(id) ? 'bg-emerald-600' : 'bg-gray-200'
                  }`}
                />
              ))}
            </div>
            <p className="shrink-0 text-xs font-medium text-gray-600" aria-live="polite">
              {fill(t('auth.consent.progress', '{done} of {total} confirmed'), {
                done: String(checkedCount),
                total: String(required.length),
              })}
            </p>
          </div>
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
            <p className="text-center text-xs text-gray-500">
              {t(
                'auth.consent.all_required',
                legal
                  ? 'All confirmations are required to continue.'
                  : 'All four confirmations are required to continue.',
              )}
            </p>
          )}
        </div>
      </div>
    </AuthPageFrame>
  );
}

/** Why the confirmations under the terms are still locked. */
function ScrollHint({ children }: { children: React.ReactNode }): JSX.Element {
  return (
    <p className="flex items-center gap-1.5 text-xs text-gray-500">
      <svg
        className="h-3.5 w-3.5 shrink-0 animate-bounce"
        viewBox="0 0 20 20"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M10 4v12m0 0-4.5-4.5M10 16l4.5-4.5" />
      </svg>
      {children}
    </p>
  );
}

/** The link to the whole published text, which both lists place under the terms. */
function FullTermsLink(): JSX.Element {
  const t = useT();
  return (
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
  );
}

/**
 * The module's confirmations, under its own terms.
 *
 * One section: the text, then every confirmation, each unlocked by reading to
 * the end — the step cannot tell which of a module's items are about the text,
 * so it treats all of them as the console treats its terms checkbox.
 */
function ModuleConfirmations({
  items,
  terms,
  termsRead,
  confirm,
}: {
  items: ConsentItems;
  terms: React.ReactNode;
  termsRead: boolean;
  confirm: Confirm;
}): JSX.Element {
  const t = useT();
  const descriptionId = useId();
  return (
    <ConsentBlock title={t('auth.consent.terms_title', 'Terms of Service')}>
      <p>
        {t(
          'auth.consent.terms_body',
          'Please read the terms in full. The confirmations below unlock once you reach the end.',
        )}
      </p>
      {terms}
      <FullTermsLink />
      {items.map((item, index) => {
        const describedBy = item.description ? `${descriptionId}-${index}` : undefined;
        return (
          <div key={item.id}>
            <label
              className={confirmationClassName(confirm(item.id).checked, termsRead)}
              data-auth="consent"
            >
              <input
                type="checkbox"
                className={checkboxClassName}
                {...confirm(item.id)}
                disabled={!termsRead}
                aria-describedby={describedBy}
              />
              <span>{item.label}</span>
            </label>
            {item.description ? (
              <p id={describedBy} className="mt-1 pl-10 text-xs text-gray-500">
                {item.description}
              </p>
            ) : null}
          </div>
        );
      })}
      {!termsRead && (
        <ScrollHint>
          {t(
            'auth.consent.terms_scroll_hint',
            'Scroll to the end of the terms to enable the confirmations.',
          )}
        </ScrollHint>
      )}
    </ConsentBlock>
  );
}

/** The console's four confirmations, about the console's terms. */
function ConsoleConfirmations({
  terms,
  termsRead,
  confirm,
}: {
  terms: React.ReactNode;
  termsRead: boolean;
  confirm: Confirm;
}): JSX.Element {
  const t = useT();
  const branding = useBranding();
  // The contact sentence names an address only when the deployment declared
  // one; both wordings stay whole sentences so a translation can reorder them.
  const researchContact = branding.contactEmail
    ? fill(
        t(
          'auth.consent.research_contact',
          'If you have questions, concerns, or complaints about the research, or feel that taking part has harmed you, please reach out to the research team at {contact_email}.',
        ),
        { contact_email: branding.contactEmail },
      )
    : t(
        'auth.consent.research_contact_fallback',
        'If you have questions, concerns, or complaints about the research, or feel that taking part has harmed you, please reach out to the research team through the operator of this deployment.',
      );
  return (
    <>
      <ConsentBlock
        step={1}
        done={confirm('age').checked}
        title={t('auth.consent.age_title', 'Age requirement')}
      >
        <p>
          {fill(
            t('auth.consent.age_body', '{app_name} is available only to adults age 18 or older.'),
            { app_name: branding.appName },
          )}
        </p>
        <label className={confirmationClassName(confirm('age').checked, true)} data-auth="consent">
          <input type="checkbox" className={checkboxClassName} {...confirm('age')} />
          <span>{t('auth.consent.age_confirm', 'I confirm that I am at least 18 years old.')}</span>
        </label>
      </ConsentBlock>

      <ConsentBlock
        step={2}
        done={confirm('terms').checked}
        title={t('auth.consent.terms_title', 'Terms of Service')}
      >
        <p>
          {t(
            'auth.consent.terms_body',
            'Please read the terms in full. The checkbox unlocks once you reach the end.',
          )}
        </p>
        {terms}
        <FullTermsLink />
        <label
          className={confirmationClassName(confirm('terms').checked, termsRead)}
          data-auth="consent"
        >
          <input
            type="checkbox"
            className={checkboxClassName}
            {...confirm('terms')}
            disabled={!termsRead}
          />
          <span>{t('auth.consent.terms_agree', 'I agree to the Terms of Service.')}</span>
        </label>
        {!termsRead && (
          <ScrollHint>
            {t(
              'auth.consent.terms_scroll_hint',
              'Scroll to the end of the terms to enable this checkbox.',
            )}
          </ScrollHint>
        )}
      </ConsentBlock>

      <ConsentBlock
        step={3}
        done={confirm('research').checked}
        title={t('auth.consent.research_title', 'Research participation')}
      >
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
        <p>{researchContact}</p>
        <label
          className={confirmationClassName(confirm('research').checked, true)}
          data-auth="consent"
        >
          <input type="checkbox" className={checkboxClassName} {...confirm('research')} />
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

      <ConsentBlock
        step={4}
        done={confirm('sharing').checked}
        title={t('auth.consent.sharing_title', 'Research data sharing')}
      >
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
            'auth.consent.sharing_future_use',
            'Your sanitized information may be used in future research studies or shared with other researchers for future studies without asking for your informed consent again.',
          )}
        </p>
        <p>
          {t(
            'auth.consent.sharing_body_3',
            'Once de-identified data have been publicly released, it may no longer be possible to withdraw or delete those copies.',
          )}
        </p>
        <label
          className={confirmationClassName(confirm('sharing').checked, true)}
          data-auth="consent"
        >
          <input type="checkbox" className={checkboxClassName} {...confirm('sharing')} />
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
    </>
  );
}
