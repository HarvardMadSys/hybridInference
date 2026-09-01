import { NextResponse, type NextRequest } from 'next/server';

const WEB_PREFIX = '/agents';
const API_PREFIX = '/agents/api';

const HOP_BY_HOP_HEADERS = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'proxy-connection',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
]);

const REQUEST_ONLY_HEADERS = new Set(['content-length', 'host']);
const RESPONSE_ONLY_HEADERS = new Set(['content-encoding', 'content-length']);

type RouteTarget = {
  base: URL;
  publicPrefix: typeof API_PREFIX | typeof WEB_PREFIX;
  upstreamPath: string;
};

type RuntimeTargets = {
  web: URL;
  api: URL;
};

function unavailable(status: 404 | 502): NextResponse {
  return new NextResponse(status === 404 ? 'Not found.' : 'Agent service is not reachable.', {
    status,
    headers: {
      'cache-control': 'no-store',
      'content-type': 'text/plain; charset=utf-8',
    },
  });
}

function configuredTargets(): { web: string; api: string } | null {
  const web = process.env.AGENT_WEB_INTERNAL_URL?.trim();
  const api = process.env.AGENT_CONTROL_PLANE_INTERNAL_URL?.trim();
  return web && api ? { web, api } : null;
}

function parseHttpTarget(raw: string): URL {
  const target = new URL(raw);
  if (target.protocol !== 'http:' && target.protocol !== 'https:') {
    throw new TypeError('Agent target must use HTTP or HTTPS.');
  }
  target.hash = '';
  target.search = '';
  target.pathname = target.pathname.replace(/\/+$/, '') || '/';
  return target;
}

function basePath(target: URL): string {
  return target.pathname === '/' ? '' : target.pathname;
}

function joinTarget(base: URL, pathname: string, search: string): URL {
  const target = new URL(base);
  target.pathname = `${basePath(base)}${pathname}` || '/';
  target.search = search;
  return target;
}

function routeTarget(request: NextRequest, targets: RuntimeTargets): RouteTarget | null {
  const { pathname } = request.nextUrl;

  if (pathname === API_PREFIX || pathname.startsWith(`${API_PREFIX}/`)) {
    const suffix = pathname.slice(API_PREFIX.length) || '/';
    return {
      base: targets.api,
      publicPrefix: API_PREFIX,
      upstreamPath: suffix,
    };
  }

  if (pathname === WEB_PREFIX || pathname.startsWith(`${WEB_PREFIX}/`)) {
    return {
      base: targets.web,
      publicPrefix: WEB_PREFIX,
      upstreamPath: pathname,
    };
  }

  return null;
}

function connectionHeaders(headers: Headers): Set<string> {
  return new Set(
    (headers.get('connection') || '')
      .split(',')
      .map((name) => name.trim().toLowerCase())
      .filter(Boolean),
  );
}

function requestHeaders(request: NextRequest): Headers {
  const headers = new Headers();
  const connectionScoped = connectionHeaders(request.headers);

  request.headers.forEach((value, name) => {
    const lower = name.toLowerCase();
    if (
      HOP_BY_HOP_HEADERS.has(lower) ||
      REQUEST_ONLY_HEADERS.has(lower) ||
      connectionScoped.has(lower) ||
      lower === 'forwarded' ||
      lower === 'x-forwarded-host' ||
      lower === 'x-forwarded-port' ||
      lower === 'x-forwarded-proto'
    ) {
      return;
    }
    headers.append(name, value);
  });

  const host = request.headers.get('host') || request.nextUrl.host;
  if (host) headers.set('x-forwarded-host', host);
  headers.set('x-forwarded-proto', request.nextUrl.protocol.replace(':', ''));
  headers.set(
    'x-forwarded-port',
    request.nextUrl.port || (request.nextUrl.protocol === 'https:' ? '443' : '80'),
  );
  return headers;
}

function stripBasePath(pathname: string, basePath: string): string {
  if (!basePath) return pathname;
  if (pathname === basePath) return '/';
  if (pathname.startsWith(`${basePath}/`)) return pathname.slice(basePath.length);
  return pathname;
}

function redirectTarget(
  redirect: URL,
  selected: RouteTarget,
  targets: RuntimeTargets,
): Pick<RouteTarget, 'base' | 'publicPrefix'> | null {
  const known = [
    { base: targets.web, publicPrefix: WEB_PREFIX },
    { base: targets.api, publicPrefix: API_PREFIX },
  ] as const;
  const exact = known.filter(({ base }) => redirect.origin === base.origin);
  if (exact.length === 1) return exact[0];
  if (exact.length > 1) return selected;
  if (redirect.hostname === selected.base.hostname) return selected;

  const sameHostname = known.filter(({ base }) => redirect.hostname === base.hostname);
  return sameHostname.length === 1 ? sameHostname[0] : null;
}

function foldInternalRedirect(
  location: string,
  upstreamRequestUrl: URL,
  selected: RouteTarget,
  targets: RuntimeTargets,
): string | null {
  let redirect: URL;
  try {
    // A path-relative Location is relative to the resource that produced it,
    // not to the service root. Using the effective fetch URL preserves deep
    // paths such as `/agents/projects/42/` + `login`.
    redirect = new URL(location, upstreamRequestUrl);
  } catch {
    return null;
  }

  // Either service may redirect to the other, and a service may name itself on
  // a different internal port. None of those Docker addresses may reach a
  // browser, so fold every known target back through its public prefix.
  const target = redirectTarget(redirect, selected, targets);
  if (!target) return null;

  const upstreamPath = stripBasePath(redirect.pathname, basePath(target.base));
  const publicPath =
    target.publicPrefix === API_PREFIX
      ? `${API_PREFIX}${upstreamPath === '/' ? '/' : upstreamPath}`
      : upstreamPath;
  return `${publicPath || '/'}${redirect.search}${redirect.hash}`;
}

function responseHeaders(
  upstream: Response,
  upstreamRequestUrl: URL,
  target: RouteTarget,
  targets: RuntimeTargets,
): Headers {
  const headers = new Headers();
  const connectionScoped = connectionHeaders(upstream.headers);

  upstream.headers.forEach((value, name) => {
    const lower = name.toLowerCase();
    if (
      HOP_BY_HOP_HEADERS.has(lower) ||
      RESPONSE_ONLY_HEADERS.has(lower) ||
      connectionScoped.has(lower) ||
      lower === 'set-cookie'
    ) {
      return;
    }
    headers.append(name, value);
  });

  for (const cookie of upstream.headers.getSetCookie()) {
    headers.append('set-cookie', cookie);
  }

  const location = headers.get('location');
  if (location) {
    const folded = foldInternalRedirect(location, upstreamRequestUrl, target, targets);
    if (folded) headers.set('location', folded);
  }

  return headers;
}

async function handle(request: NextRequest): Promise<Response> {
  const configured = configuredTargets();
  if (!configured) return unavailable(404);

  let targets: RuntimeTargets;
  let target: RouteTarget;
  let url: URL;
  try {
    targets = { web: parseHttpTarget(configured.web), api: parseHttpTarget(configured.api) };
    const selected = routeTarget(request, targets);
    if (!selected) return unavailable(404);
    target = selected;
    url = joinTarget(target.base, target.upstreamPath, request.nextUrl.search);
  } catch {
    return unavailable(502);
  }

  const hasBody = request.method !== 'GET' && request.method !== 'HEAD' && request.body !== null;
  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method: request.method,
      headers: requestHeaders(request),
      body: hasBody ? request.body : undefined,
      ...(hasBody ? { duplex: 'half' } : {}),
      redirect: 'manual',
      cache: 'no-store',
      signal: request.signal,
    } as RequestInit);
  } catch {
    return unavailable(502);
  }

  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: responseHeaders(upstream, url, target, targets),
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
