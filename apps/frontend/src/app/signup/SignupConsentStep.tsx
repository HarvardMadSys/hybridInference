'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { useBranding } from '@/components/providers/SiteConfigProvider';
import { TermsSections } from '@/app/terms/TermsContent';

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
  return (
    <section className="rounded-lg border border-gray-200 bg-gray-50 px-4 py-4">
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
    <div className="mx-auto w-full max-w-md">
      <Card>
        <div className="text-center">
          <h1 className="text-3xl font-bold tracking-tight text-gray-900">
            Before you create an account
          </h1>
          <p className="mt-2 text-sm text-gray-600">
            {branding.appName} is an experimental research service. Please read and confirm each
            item below.
          </p>
        </div>

        <div className="mt-8 space-y-4">
          <ConsentBlock step={1} title="Age requirement">
            <p>{branding.appName} is available only to adults age 18 or older.</p>
            <label className="flex items-start gap-3 font-medium text-gray-900">
              <input
                type="checkbox"
                className={checkboxClassName}
                checked={consent.age}
                onChange={toggle('age')}
              />
              <span>I confirm that I am at least 18 years old.</span>
            </label>
          </ConsentBlock>

          <ConsentBlock step={2} title="Terms of Service">
            <p>Please read the terms in full. The checkbox unlocks once you reach the end.</p>
            <div
              ref={termsRef}
              role="region"
              aria-label="Terms of Service"
              tabIndex={0}
              className="max-h-56 overflow-y-auto rounded-md border border-gray-200 bg-white px-4 py-3 focus:outline-none focus:ring-2 focus:ring-blue-500/40"
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
                Open the full Terms of Service in a new tab
              </Link>
            </p>
            <label
              className={`flex items-start gap-3 font-medium ${
                termsRead ? 'text-gray-900' : 'text-gray-400'
              }`}
            >
              <input
                type="checkbox"
                className={checkboxClassName}
                checked={consent.terms}
                disabled={!termsRead}
                onChange={toggle('terms')}
              />
              <span>I agree to the Terms of Service.</span>
            </label>
            {!termsRead && (
              <p className="text-xs text-gray-500">
                Scroll to the end of the terms to enable this checkbox.
              </p>
            )}
          </ConsentBlock>

          <ConsentBlock step={3} title="Research participation">
            <p>
              {branding.appName} is operated to study how people and software agents use large
              language models. If you participate, we may collect and analyze:
            </p>
            <ul className="list-disc space-y-0.5 pl-5">
              <li>prompts sent through {branding.appName};</li>
              <li>model responses;</li>
              <li>tool calls and tool outputs;</li>
              <li>model and provider information;</li>
              <li>timestamps and request/session information;</li>
              <li>token counts, latency, routing, and other usage metadata.</li>
            </ul>
            <p>
              These data may be used by the research team to characterize LLM workloads, evaluate
              serving systems, and publish research results.
            </p>
            <p className="font-medium">
              Do not submit passwords, credentials, confidential information, regulated data, or
              sensitive personal information.
            </p>
            <p>
              Participation is voluntary. If you do not agree, you cannot use the research service.
            </p>
            <p>
              {branding.contactEmail
                ? `If you have questions, concerns, or complaints about the research, or feel that taking part has harmed you, please reach out to the research team at ${branding.contactEmail}.`
                : 'If you have questions, concerns, or complaints about the research, or feel that taking part has harmed you, please reach out to the research team through the operator of this deployment.'}
            </p>
            <label className="flex items-start gap-3 font-medium text-gray-900">
              <input
                type="checkbox"
                className={checkboxClassName}
                checked={consent.research}
                onChange={toggle('research')}
              />
              <span>
                I consent to participate in this research and to the collection and analysis of my{' '}
                {branding.appName} usage data.
              </span>
            </label>
          </ConsentBlock>

          <ConsentBlock step={4} title="Research data sharing">
            <p>
              Some data from this study may be included in research publications or released as a
              research dataset. Released data may include sanitized prompts and responses, tool
              calls and outputs, timing information, model and framework information, and usage and
              performance metadata.
            </p>
            <p>
              Before public release, we process the data to remove or redact direct identifiers and
              detected personally identifiable information. Automated sanitization cannot guarantee
              removal of every sensitive or identifying detail.
            </p>
            <p>
              Your sanitized information may be used in future research studies or shared with other
              researchers for future studies without asking for your informed consent again.
            </p>
            <p>
              Once de-identified data have been publicly released, it may no longer be possible to
              withdraw or delete those copies.
            </p>
            <label className="flex items-start gap-3 font-medium text-gray-900">
              <input
                type="checkbox"
                className={checkboxClassName}
                checked={consent.sharing}
                onChange={toggle('sharing')}
              />
              <span>
                I understand and consent to the sharing and possible public release of de-identified
                research data derived from my {branding.appName} usage.
              </span>
            </label>
          </ConsentBlock>
        </div>

        <div className="mt-6 space-y-4">
          <Button type="button" className="w-full" disabled={!allChecked} onClick={onContinue}>
            Continue
          </Button>
          {!allChecked && (
            <p className="text-center text-xs text-gray-500">
              All four confirmations are required to continue.
            </p>
          )}
          <div className="text-center text-sm text-gray-600">
            Already have an account?{' '}
            <a className="font-medium text-blue-600 hover:text-blue-700" href="/login">
              Log In
            </a>
          </div>
        </div>
      </Card>
    </div>
  );
}
