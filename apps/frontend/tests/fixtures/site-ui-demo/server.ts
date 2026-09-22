import type { SiteUiServerModule } from '@site-ui/host';

/** The fixture sets a document language without importing client components. */
export const locale = 'en-GB';

const server: SiteUiServerModule = { locale };

export default server;
