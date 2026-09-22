import type { SiteUiServerModule } from '@site-ui/host';

/**
 * The fixture's server half.
 *
 * Read by the root layout during a fixture build. It declares a locale and a
 * title so a test can prove the host actually reads them — the neutral module
 * declares neither, and "both halves work" is only true if one of them does.
 *
 * Never `import` the client half from here: that is a component tree, and a
 * server module that pulled it in would drag hooks into a request-less context.
 * The interface keeps the two entries separate for exactly this reason.
 */
export const locale = 'en-GB';

export const metadata = {
  title: 'Site UI fixture',
  description: 'A tiny module used to prove the Site UI API v1 seam.',
};

const server: SiteUiServerModule = { locale, metadata };

export default server;
