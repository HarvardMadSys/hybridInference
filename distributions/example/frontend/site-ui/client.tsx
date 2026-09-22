'use client';

import type { LandingPageProps } from '@site-ui/host';

/**
 * The smallest useful distribution module.
 *
 * Its whole job is to show what "replace one page" looks like, because that is
 * what most deployments want and the alternative — starting from a module that
 * reimplements everything — teaches the wrong shape. It exports `descriptor` and
 * `Landing` and deliberately declines everything else:
 *
 * - no `AuthFrame`, so the account pages keep the console's own card and chrome;
 * - no `TermsFrame`, so the legal page keeps the console's;
 * - no `authMessages` and no `authAppearance`, so the shared forms are untouched;
 * - no `metadata`, because nothing about the document head changes;
 * - no `PublicFrame`, which the host never renders anyway.
 *
 * `descriptor` and `Landing` are the entire module, and that is a complete,
 * valid one: every other export is optional because each is an answer to a
 * question the host asks, and this module's answer to all of them is "the
 * console's own page is correct".
 *
 * Every one of those omissions is a statement rather than a gap, and the host
 * reads it that way: the routes this module does not claim are rendered exactly
 * as they would be with no module at all. A deployment that wants its own
 * sign-in page adds an `AuthFrame`; until then it has the console's.
 */

export const descriptor = {
  siteUiApi: 1,
  id: 'example',
  locale: 'en',
} as const;

/**
 * The home page. A heading, one sentence, and a way into the console.
 *
 * It draws a complete page — `<header>`, `<main>`, `<footer>` — because a
 * `Landing` is a claim to own `/` entirely, and the rule is the same one
 * `AuthFrame` and `TermsFrame` carry: supplying a frame means drawing the chrome.
 * A module that rendered only a body would leave the page with no landmark to
 * navigate by, and nothing in the host would correct it.
 */
export function Landing(_props: LandingPageProps) {
  return (
    <>
      <header className="mx-auto flex w-full max-w-3xl items-center justify-between px-6 py-6">
        <span className="font-semibold">Example deployment</span>
        <a className="text-sm underline" href="/login">
          Sign in
        </a>
      </header>
      <main className="mx-auto flex min-h-[70vh] w-full max-w-3xl flex-col justify-center gap-6 px-6">
      <img
        src="/site-assets/example/mark.svg"
        alt=""
        width={64}
        height={64}
        // A plain `img` and not `next/image`: the module ships this file itself,
        // and the static import path a bundler would rewrite is exactly what the
        // asset contract keeps out of a module's hands.
        // eslint-disable-next-line @next/next/no-img-element
      />
      <h1 className="text-4xl font-bold tracking-tight">Example deployment</h1>
      <p className="text-lg text-gray-600">
        This page comes from a UI module in the deployment&apos;s own repository. Everything
        else — sign-in, sign-up, the console, the terms page — is the shared application.
      </p>
        <a
          className="w-fit rounded-md bg-gray-900 px-5 py-3 font-medium text-white"
          href="/dashboard"
        >
          Open the console
        </a>
      </main>
      <footer className="mx-auto w-full max-w-3xl px-6 py-6 text-sm text-gray-500">
        Everything else on this site is the shared application.
      </footer>
    </>
  );
}
