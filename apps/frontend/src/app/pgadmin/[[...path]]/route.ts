/**
 * Admin-gated reverse proxy for pgAdmin.
 *
 * Why this lives in the console rather than in Nginx: public traffic reaches
 * this app first and the console's own rewrite table (next.config.js) is what
 * actually routes `/v1/`, `/auth/`, `/health` and friends on to FastAPI. Nginx
 * still holds a `/pgadmin/` block with an `auth_request` gate, but nothing
 * public goes through Nginx any more, so that block never runs — which is why
 * clicking pgAdmin in the dashboard returned the console's own 404 page.
 *
 * A rewrite alone cannot replace it: rewrites cannot authenticate. Whether
 * pgAdmin also asks for a login depends on the host — `SERVER_MODE` defaults
 * to False in the pgadmin service, and a host that sets it True gains a second
 * gate. This code cannot tell which, so it assumes it is the only thing in
 * front of a database console: it fails closed on every unexpected condition
 * rather than falling through.
 */
import { NextResponse, type NextRequest } from 'next/server';

const BACKEND_INTERNAL_URL = process.env.BACKEND_INTERNAL_URL || 'http://backend:8080';
const PGADMIN_INTERNAL_URL = process.env.PGADMIN_INTERNAL_URL || 'http://pgadmin:80';

/**
 * Must match `SCRIPT_NAME` on the pgadmin service in
 * deploy/docker/docker-compose.yml — pgAdmin builds its own asset and redirect
 * URLs from it, so the two have to agree or every page loads without styles.
 */
const PGADMIN_SCRIPT_NAME = '/pgadmin';

/** Budget for the admin check. Exceeding it denies, it does not admit. */
const VERIFY_TIMEOUT_MS = 5_000;

/** Session cookie issued by FastAPI; pgAdmin has no use for it. */
const SESSION_COOKIE = 'refresh_token';

/**
 * Hop-by-hop headers apply to a single connection and must not be relayed
 * (RFC 9110 §7.6.1). `host` and `content-length` are dropped alongside them
 * because fetch derives both from the outgoing request.
 */
const UNFORWARDABLE_HEADERS = new Set([
  'connection',
  'content-length',
  'host',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
]);

type Verdict = 'admin' | 'unauthenticated' | 'forbidden';

/**
 * Ask FastAPI whether the caller's session belongs to an admin.
 *
 * Every outcome that is not an explicit 200 denies. A backend that is down,
 * slow, or answering something unexpected is not a reason to hand out a
 * database console, and this is the only check standing in front of one.
 */
async function verifyAdmin(cookie: string | null): Promise<Verdict> {
  if (!cookie) return 'unauthenticated';

  let response: Response;
  try {
    response = await fetch(`${BACKEND_INTERNAL_URL}/internal/verify-admin`, {
      headers: { cookie },
      cache: 'no-store',
      signal: AbortSignal.timeout(VERIFY_TIMEOUT_MS),
    });
  } catch {
    return 'forbidden';
  }

  if (response.status === 200) return 'admin';
  if (response.status === 401) return 'unauthenticated';
  return 'forbidden';
}

/** Drop the console's own session cookie; pgAdmin keeps its own separately. */
function cookiesForUpstream(cookie: string | null): string | null {
  if (!cookie) return null;
  const kept = cookie
    .split(';')
    .map((part) => part.trim())
    .filter((part) => part.length > 0 && !part.toLowerCase().startsWith(`${SESSION_COOKIE}=`));
  return kept.length > 0 ? kept.join('; ') : null;
}

function requestHeaders(request: NextRequest): Headers {
  const headers = new Headers();
  request.headers.forEach((value, name) => {
    if (!UNFORWARDABLE_HEADERS.has(name.toLowerCase())) headers.set(name, value);
  });

  const cookie = cookiesForUpstream(request.headers.get('cookie'));
  if (cookie) {
    headers.set('cookie', cookie);
  } else {
    headers.delete('cookie');
  }

  headers.set('x-forwarded-proto', request.nextUrl.protocol.replace(':', ''));
  const host = request.headers.get('host');
  if (host) headers.set('x-forwarded-host', host);

  return headers;
}

/**
 * Fold a redirect that points at the upstream itself back to a bare path.
 *
 * pgAdmin should emit relative redirects, but Werkzeug can be configured to
 * build absolute ones from the Host header it saw — which here is the
 * container name, and resolves nowhere in a browser. Derived from the
 * configured upstream rather than a literal so it still holds when
 * PGADMIN_INTERNAL_URL is overridden. Returns null to leave the value alone.
 */
function foldUpstreamRedirect(location: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(location, PGADMIN_INTERNAL_URL);
  } catch {
    return null;
  }
  if (parsed.origin !== new URL(PGADMIN_INTERNAL_URL).origin) return null;
  return `${parsed.pathname}${parsed.search}${parsed.hash}`;
}

function responseHeaders(upstream: Response): Headers {
  const headers = new Headers();
  upstream.headers.forEach((value, name) => {
    const lower = name.toLowerCase();
    if (UNFORWARDABLE_HEADERS.has(lower)) return;
    // fetch has already decompressed the body, so relaying the encoding it
    // arrived under would leave the browser trying to gunzip plain bytes.
    if (lower === 'content-encoding' || lower === 'set-cookie') return;
    headers.set(name, value);
  });
  // forEach folds repeated Set-Cookie into one value; getSetCookie keeps them apart.
  for (const cookie of upstream.headers.getSetCookie()) headers.append('set-cookie', cookie);

  const location = headers.get('location');
  if (location) {
    const folded = foldUpstreamRedirect(location);
    if (folded) headers.set('location', folded);
  }

  return headers;
}

function denied(request: NextRequest, verdict: Verdict): NextResponse {
  const isNavigation = request.method === 'GET' || request.method === 'HEAD';
  if (verdict === 'unauthenticated' && isNavigation) {
    return NextResponse.redirect(new URL('/login', request.nextUrl), 302);
  }
  return new NextResponse('Admin access required.', {
    status: 403,
    headers: { 'content-type': 'text/plain; charset=utf-8' },
  });
}

async function handle(request: NextRequest): Promise<Response> {
  const verdict = await verifyAdmin(request.headers.get('cookie'));
  if (verdict !== 'admin') return denied(request, verdict);

  // The console mounts this route at the same prefix pgAdmin serves itself
  // under, so the incoming path passes through unchanged — no rewriting, and
  // no re-encoding of segments the router already decoded.
  const { pathname, search } = request.nextUrl;
  if (pathname !== PGADMIN_SCRIPT_NAME && !pathname.startsWith(`${PGADMIN_SCRIPT_NAME}/`)) {
    return new NextResponse('Not found.', { status: 404 });
  }

  const hasBody = request.method !== 'GET' && request.method !== 'HEAD';
  let upstream: Response;
  try {
    upstream = await fetch(new URL(pathname + search, PGADMIN_INTERNAL_URL), {
      method: request.method,
      headers: requestHeaders(request),
      body: hasBody ? request.body : undefined,
      // Streaming a request body requires half duplex; the option is not yet
      // in the DOM RequestInit type that Next ships.
      ...(hasBody ? { duplex: 'half' } : {}),
      redirect: 'manual',
      cache: 'no-store',
    } as RequestInit);
  } catch {
    // pgAdmin runs behind the `admin` Compose profile, so the usual cause is
    // a deployment that did not start it. Say so rather than leaking a stack.
    return new NextResponse('pgAdmin is not reachable.', {
      status: 502,
      headers: { 'content-type': 'text/plain; charset=utf-8' },
    });
  }

  return new NextResponse(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders(upstream),
  });
}

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

export {
  handle as GET,
  handle as HEAD,
  handle as POST,
  handle as PUT,
  handle as PATCH,
  handle as DELETE,
  handle as OPTIONS,
};
