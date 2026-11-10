// The console's own liveness check, for the container's HEALTHCHECK
// (deploy/docker/Dockerfile.frontend).
//
// It answers without asking the gateway. The check used to load `/`, which
// renders a whole page from the gateway's /site-config: every 15 seconds that
// was a render and a gateway call, and a gateway outage marked the console
// unhealthy with it. The console deliberately does not depend on the gateway's
// health -- Compose starts it on `service_started` -- and `/health` is the
// gateway's own check, forwarded.
export const dynamic = 'force-dynamic';

export function GET(): Response {
  return new Response('ok\n', {
    headers: { 'cache-control': 'no-store', 'content-type': 'text/plain; charset=utf-8' },
  });
}
