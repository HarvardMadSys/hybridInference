// Alert history from sources outside this network (the status monitor), on the
// gateway. Exactly this path, not a prefix: it admits only a scoped writer
// credential and answers 404 while alert history is off. Reads stay under
// `/admin`.
import { gatewayRoute } from '@/lib/server/gatewayProxy';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

const relay = gatewayRoute('/internal/alert-history/events');

export {
  relay as GET,
  relay as HEAD,
  relay as POST,
  relay as PUT,
  relay as PATCH,
  relay as DELETE,
  relay as OPTIONS,
};
