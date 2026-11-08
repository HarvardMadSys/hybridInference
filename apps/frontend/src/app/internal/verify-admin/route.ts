// The cookie-session admin check, on the gateway. Each `/internal` path is
// published by name, never as a prefix: see docs/developer/public-path-table.md.
import { gatewayRoute } from '@/lib/server/gatewayProxy';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

const relay = gatewayRoute('/internal/verify-admin');

export {
  relay as GET,
  relay as HEAD,
  relay as POST,
  relay as PUT,
  relay as PATCH,
  relay as DELETE,
  relay as OPTIONS,
};
