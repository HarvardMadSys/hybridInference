'use client';

import { useBranding } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/useT';
import { fill } from '@/lib/utils/interpolate';

/**
 * The legal text, and the body a distribution's legal frame wraps.
 *
 * This is the **console's** legal text: English, the sentences the console
 * ships. A distribution that publishes its own terms owns that text in its own
 * module, together with the frame it renders in — a layout and its copy travel
 * together, which is why this component is not handed to a module's frame.
 *
 * The one thing the two must agree on is the anchor prefix, and that is why
 * `TERMS_SECTION_ANCHOR` lives in the contract: the console's footer, the
 * account pages and the sign-up consent step all link to `/terms#terms-s5`, so
 * a module that invented its own prefix would leave every one of those links
 * pointing at nothing.
 *
 * A client component, because the table of contents is a `<details>` the reader
 * opens and the sign-up step embeds these sections live.
 */

function useTermsSections(): { title: string; body: string[] }[] {
  const branding = useBranding();
  const t = useT();
  // The contact paragraph names the operator only when the deployment declared
  // one; both wordings stay whole sentences so a translation can reorder them.
  let contactBody: string;
  if (branding.contactEmail) {
    contactBody = fill(
      t(
        'terms.s10.body_1',
        'For questions about {app_name} or these terms, contact us at {contact_email}.',
      ),
      { app_name: branding.appName, contact_email: branding.contactEmail },
    );
  } else {
    contactBody = fill(
      t(
        'terms.s10.body_2',
        'For questions about {app_name} or these terms, contact the operator of this deployment.',
      ),
      { app_name: branding.appName },
    );
  }
  return [
    {
      title: t('terms.s1.title', '1. Service Overview'),
      body: [
        fill(
          t(
            'terms.s1.body_1',
            '{app_name} is an experimental research service that provides access to hosted and routed large language model inference for experimentation and related development work.',
          ),
          { app_name: branding.appName },
        ),
        t(
          'terms.s1.body_2',
          'The service may route requests across local inference servers and remote model providers. Available models, providers, limits, features, latency, throughput, output quality, and routing behavior may change without notice, and there is no performance guarantee.',
        ),
      ],
    },
    {
      title: t('terms.s2.title', '2. Eligibility and Accounts'),
      body: [
        fill(
          t(
            'terms.s2.body_1',
            'You must be at least 18 years old to create an account or use {app_name}. By creating an account or using the service, you confirm that you are at least 18 years old and have the legal capacity to agree to these Terms.',
          ),
          { app_name: branding.appName },
        ),
        t(
          'terms.s2.body_2',
          'You are responsible for maintaining the confidentiality of your account credentials and API keys. You are responsible for activity submitted through your account or keys.',
        ),
        t(
          'terms.s2.body_3',
          "Do not share API keys publicly, embed them in client-side code, or use another person's account without permission.",
        ),
      ],
    },
    {
      title: t('terms.s3.title', '3. Acceptable Use'),
      body: [
        t(
          'terms.s3.body_1',
          'You may not use the service to violate laws, infringe rights, compromise security, abuse infrastructure, or intentionally disrupt availability for other users.',
        ),
        t(
          'terms.s3.body_2',
          'You may not attempt to bypass rate limits, quotas, authentication, authorization, routing controls, or provider restrictions.',
        ),
      ],
    },
    {
      title: t('terms.s4.title', '4. Quotas and Limits'),
      body: [
        t(
          'terms.s4.body_1',
          'Quotas, rate limits, model access, and usage limits may change based on usage, demand, infrastructure capacity, abuse prevention, operational needs, and individual or aggregate activity.',
        ),
        t(
          'terms.s4.body_2',
          'High-volume, automated, abusive, or operationally risky usage may be limited, delayed, deprioritized, or blocked without advance notice.',
        ),
      ],
    },
    {
      title: t('terms.s5.title', '5. Logging and Data Use'),
      body: [
        t(
          'terms.s5.body_1',
          'All prompts and responses may be logged for research purposes, stored, hashed, redacted, or otherwise processed depending on operator configuration and service needs. Logs and derived data may be used to operate, secure, debug, improve, and analyze the service.',
        ),
        t(
          'terms.s5.body_2',
          'We sanitize all text before analysis where feasible to reduce sensitive or identifying content in analysis workflows. Sanitization is not a guarantee that all sensitive information will be removed from logs, derived data, or third-party provider systems.',
        ),
        t(
          'terms.s5.body_3',
          'Logged requests are analyzed to study how the service is used, and anonymized data derived from them — such as sanitized prompts and responses, usage statistics, and routing metrics — may be published or open-sourced to support reproducible research. Any such release is intended to contain only de-identified data, but, as noted above, sanitization cannot guarantee that all sensitive information is removed.',
        ),
        t(
          'terms.s5.body_4',
          'Do not submit sensitive personal information, confidential information, regulated data, secrets, credentials, or data you are not authorized to process through the service.',
        ),
      ],
    },
    {
      title: t('terms.s6.title', '6. Third-Party Providers'),
      body: [
        t(
          'terms.s6.body_1',
          'Some requests may be forwarded to third-party model providers. Those providers may process submitted prompts, responses, metadata, and related usage information under their own terms and policies.',
        ),
        t(
          'terms.s6.body_2',
          'You are responsible for ensuring that your use of the service and any routed providers is appropriate for your data and intended use case.',
        ),
      ],
    },
    {
      title: t('terms.s7.title', '7. No Warranty'),
      body: [
        t(
          'terms.s7.body_1',
          'The service is provided without guarantee and on an as-is and as-available basis. Outputs may be inaccurate, incomplete, unsafe, unavailable, delayed, or unsuitable for your needs.',
        ),
        t(
          'terms.s7.body_2',
          'You must review model outputs before relying on them. Do not rely on the service for legal, medical, financial, safety-critical, or other high-risk decisions. This page is not legal advice.',
        ),
      ],
    },
    {
      title: t('terms.s8.title', '8. Suspension and Termination'),
      body: [
        t(
          'terms.s8.body_1',
          'Access may be limited, suspended, or terminated for misuse, security concerns, operational needs, policy violations, or discontinuation of the service.',
        ),
        t(
          'terms.s8.body_2',
          'We may remove or restrict models, providers, accounts, API keys, or features at any time.',
        ),
      ],
    },
    {
      title: t('terms.s9.title', '9. Changes to These Terms'),
      body: [
        t(
          'terms.s9.body_1',
          'These terms may be updated from time to time. Continued use of the service after updates means you accept the revised terms.',
        ),
      ],
    },
    {
      title: t('terms.s10.title', '10. Contact'),
      body: [contactBody],
    },
  ];
}

/**
 * The numbered sections of the terms, without the page header. Shared by the
 * /terms page and the signup consent step so the text can't drift.
 */
export function TermsSections({
  headingLevel = 2,
  compact = false,
  anchorPrefix,
}: {
  headingLevel?: 2 | 3;
  compact?: boolean;
  /** When set, each section gets `id={anchorPrefix}{n}` for the table of contents. */
  anchorPrefix?: string;
}): JSX.Element {
  const sections = useTermsSections();
  const Heading = headingLevel === 3 ? 'h3' : 'h2';
  return (
    <div className={compact ? 'space-y-4' : 'mt-8 space-y-8'}>
      {sections.map((section, index) => (
        <section
          key={section.title}
          id={anchorPrefix ? `${anchorPrefix}${index + 1}` : undefined}
          className={compact ? 'space-y-1.5' : 'space-y-3'}
        >
          <Heading
            className={
              compact
                ? 'text-sm font-semibold text-gray-900'
                : 'text-xl font-semibold tracking-tight text-gray-950'
            }
          >
            {section.title}
          </Heading>
          {section.body.map((paragraph, index) => (
            <p
              key={index}
              className={
                compact ? 'text-xs leading-5 text-gray-700' : 'text-sm leading-7 text-gray-700'
              }
            >
              {paragraph}
            </p>
          ))}
        </section>
      ))}
    </div>
  );
}
