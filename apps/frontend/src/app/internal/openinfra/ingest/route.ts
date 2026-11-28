// OpenInfra snapshots from the reporters on the deployment's own GPU servers,
// on the gateway. Exactly this path, not a prefix: it admits only the ingest
// token and answers 404 while no token is configured. The public page reads
// `/public-openinfra`, a rewrite.
import { gatewayRoute } from '@/lib/server/gatewayProxy';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

const relay = gatewayRoute('/internal/openinfra/ingest');

export {
  relay as GET,
  relay as HEAD,
  relay as POST,
  relay as PUT,
  relay as PATCH,
  relay as DELETE,
  relay as OPTIONS,
};
