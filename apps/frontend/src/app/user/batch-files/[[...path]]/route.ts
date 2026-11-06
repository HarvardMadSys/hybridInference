// Batch input files, on the gateway: the console's uploads and downloads.
// Forwarded by a route handler rather than the `/user/:path*` rewrite because
// an upload is large. Next copies every body it forwards through a rewrite,
// holds the copy in memory, and cuts it off at `middlewareClientMaxBodySize`
// (next.config.js) -- which is the gateway's ordinary body limit, while an
// upload may reach BATCH_MAX_BODY_MB, a limit the gateway applies to this path
// instead. A route handler streams it, in both directions: see
// src/lib/server/gatewayProxy.ts.
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
