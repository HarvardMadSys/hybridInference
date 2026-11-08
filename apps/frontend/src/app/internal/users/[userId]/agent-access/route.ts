// Whether one user may use the cloud agent, read by the agent, on the gateway.
// Each `/internal` path is published by name, never as a prefix: see
// docs/developer/public-path-table.md.
import { gatewayRoute } from '@/lib/server/gatewayProxy';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

const relay = gatewayRoute('/internal/users');

export {
  relay as GET,
  relay as HEAD,
  relay as POST,
  relay as PUT,
  relay as PATCH,
  relay as DELETE,
  relay as OPTIONS,
};
