import type { MetaMessages, SiteUiServerModule } from '@site-ui/host';

/** The fixture sets a document language without importing client components. */
export const locale = 'en-GB';

/**
 * Titles for two of the routes the fixture renders. The others keep the
 * console's, which is how a partial set of wording is meant to behave.
 */
export const metaMessages: MetaMessages = {
  'meta.terms.title': 'Demonstration terms',
  'meta.login.title': 'Sign in to the demonstration',
  'meta.login.description': 'The demonstration of {app_name}.',
};

const server: SiteUiServerModule = { locale };

export default server;
