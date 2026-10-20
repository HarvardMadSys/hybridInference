// The admin-only model playground, on the gateway.
// Forwarded by a route handler, not a rewrite, because these responses
// stream: see src/lib/server/gatewayProxy.ts.
import { proxyToGateway } from '@/lib/server/gatewayProxy';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

export {
  proxyToGateway as GET,
  proxyToGateway as HEAD,
  proxyToGateway as POST,
  proxyToGateway as PUT,
  proxyToGateway as PATCH,
  proxyToGateway as DELETE,
  proxyToGateway as OPTIONS,
};
