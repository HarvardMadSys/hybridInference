// Inference grants for the cloud agent, on the gateway: minting one, and
// renewing, reading or revoking it. The one `/internal` prefix published
// whole, because every route under it requires the dispatch token: see
// docs/developer/public-path-table.md.
import { gatewayRoute } from '@/lib/server/gatewayProxy';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

const relay = gatewayRoute('/internal/agent-grants');

export {
  relay as GET,
  relay as HEAD,
  relay as POST,
  relay as PUT,
  relay as PATCH,
  relay as DELETE,
  relay as OPTIONS,
};
