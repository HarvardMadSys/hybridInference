'use client';

import { useBranding } from '@/components/providers/SiteConfigProvider';

export function TermsContent(): JSX.Element {
  const branding = useBranding();
  const sections = [
    {
      title: '1. Service Overview',
      body: [
        `${branding.appName} is an experimental research service that provides access to hosted and routed large language model inference for experimentation and related development work.`,
        'The service may route requests across local inference servers and remote model providers. Available models, providers, limits, features, latency, throughput, output quality, and routing behavior may change without notice, and there is no performance guarantee.',
      ],
    },
    {
      title: '2. Eligibility and Accounts',
      body: [
        'You are responsible for maintaining the confidentiality of your account credentials and API keys. You are responsible for activity submitted through your account or keys.',
        "Do not share API keys publicly, embed them in client-side code, or use another person's account without permission.",
      ],
    },
    {
      title: '3. Acceptable Use',
      body: [
        'You may not use the service to violate laws, infringe rights, compromise security, abuse infrastructure, or intentionally disrupt availability for other users.',
        'You may not attempt to bypass rate limits, quotas, authentication, authorization, routing controls, or provider restrictions.',
      ],
    },
    {
      title: '4. Quotas and Limits',
      body: [
        'Quotas, rate limits, model access, and usage limits may change based on usage, demand, infrastructure capacity, abuse prevention, operational needs, and individual or aggregate activity.',
        'High-volume, automated, abusive, or operationally risky usage may be limited, delayed, deprioritized, or blocked without advance notice.',
      ],
    },
    {
      title: '5. Logging and Data Use',
      body: [
        'All prompts and responses may be logged for research purposes, stored, hashed, redacted, or otherwise processed depending on operator configuration and service needs. Logs and derived data may be used to operate, secure, debug, improve, and analyze the service.',
        'We sanitize all text before analysis where feasible to reduce sensitive or identifying content in analysis workflows. Sanitization is not a guarantee that all sensitive information will be removed from logs, derived data, or third-party provider systems.',
        'Logged requests are analyzed to study how the service is used, and anonymized data derived from them — such as sanitized prompts and responses, usage statistics, and routing metrics — may be published or open-sourced to support reproducible research. Any such release is intended to contain only de-identified data, but, as noted above, sanitization cannot guarantee that all sensitive information is removed.',
        'Do not submit sensitive personal information, confidential information, regulated data, secrets, credentials, or data you are not authorized to process through the service.',
      ],
    },
    {
      title: '6. Third-Party Providers',
      body: [
        'Some requests may be forwarded to third-party model providers. Those providers may process submitted prompts, responses, metadata, and related usage information under their own terms and policies.',
        'You are responsible for ensuring that your use of the service and any routed providers is appropriate for your data and intended use case.',
      ],
    },
    {
      title: '7. No Warranty',
      body: [
        'The service is provided without guarantee and on an as-is and as-available basis. Outputs may be inaccurate, incomplete, unsafe, unavailable, delayed, or unsuitable for your needs.',
        'You must review model outputs before relying on them. Do not rely on the service for legal, medical, financial, safety-critical, or other high-risk decisions. This page is not legal advice.',
      ],
    },
    {
      title: '8. Suspension and Termination',
      body: [
        'Access may be limited, suspended, or terminated for misuse, security concerns, operational needs, policy violations, or discontinuation of the service.',
        'We may remove or restrict models, providers, accounts, API keys, or features at any time.',
      ],
    },
    {
      title: '9. Changes to These Terms',
      body: [
        'These terms may be updated from time to time. Continued use of the service after updates means you accept the revised terms.',
      ],
    },
    {
      title: '10. Contact',
      body: [
        branding.contactEmail
          ? `For questions about ${branding.appName} or these terms, contact us at ${branding.contactEmail}.`
          : `For questions about ${branding.appName} or these terms, contact the operator of this deployment.`,
      ],
    },
  ];

  return (
    <article className="mx-auto w-full max-w-3xl rounded-2xl border border-gray-200 bg-white px-6 py-8 shadow-sm sm:px-10 sm:py-10">
      <div className="border-b border-gray-200 pb-6">
        <h1 className="text-3xl font-bold tracking-tight text-gray-950 sm:text-4xl">
          Terms of Service
        </h1>
        <p className="mt-4 text-sm leading-6 text-gray-600">
          Last updated: June 20, 2026. These terms are a practical operating policy for using{' '}
          {branding.appName} and are not legal advice.
        </p>
      </div>

      <div className="mt-8 space-y-8">
        {sections.map((section) => (
          <section key={section.title} className="space-y-3">
            <h2 className="text-xl font-semibold tracking-tight text-gray-950">{section.title}</h2>
            {section.body.map((paragraph, index) => (
              <p key={index} className="text-sm leading-7 text-gray-700">
                {paragraph}
              </p>
            ))}
          </section>
        ))}
      </div>
    </article>
  );
}
