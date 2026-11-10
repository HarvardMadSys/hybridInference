/**
 * Forwards a request to the gateway, for the paths whose responses stream.
 *
 * The other gateway paths are plain rewrites in next.config.js. These are not,
 * because Next's rewrite proxy mishandles a long-lived response (measured on
 * 15.5.27, see #1568):
 *
 * - **A gateway that goes away is never reported.** Once the response has
 *   started, the rewrite does not end it when the gateway's side drops -- a
 *   crash, a restart, a deploy -- so the caller waits until something in front
 *   of the console gives up (a 10-minute test never ended).
 * - **Thirty seconds of silence ends the request.** Its `proxyTimeout`
 *   default cuts any response the gateway is still preparing.
 * - **A failure is a bare 500** in Next's own text, not the caller's error shape.
 *
 * This proxy relays bytes exactly as they arrive and in both directions ends
 * what the other side ended: a response cut short reaches the caller as a
 * broken response, never as a complete one, and a caller that hangs up cancels
 * the gateway's work at once. It sets no deadline on a request -- the gateway
 * bounds every request (REQUEST_TIMEOUT_SECONDS, STREAM_REQUEST_TIMEOUT_SECONDS)
 * and streams send a keepalive every 15s -- only on silence: a connection that
 * carries nothing either way for UPSTREAM_IDLE_TIMEOUT_MS is closed.
 *
 * A request does not outlive its response. The gateway may answer before the
 * body has arrived -- it refuses a key on the headers alone -- and nothing then
 * finishes the request. A caller that leaves on that answer is not reported
 * gone: `request.signal` fires only for one who leaves before the response is
 * finished. For a caller that sends on, Node no longer says when the gateway's
 * connection will take more, so a body that has to wait once waits for good.
 * Nor does the idle limit apply: Node stops timing a request whose response
 * has ended. So when the gateway's response is over, a request still being
 * sent is dropped with its connection, which would otherwise stay open for
 * good, here and in the gateway.
 *
 * Connections to the gateway are kept open and reused, so each request's body
 * is framed by the bytes this proxy writes, never by what the caller declared:
 * the gateway reads a body up to where its framing says it ends, and whatever
 * follows as the next request, which may be another caller's. See
 * `bodyFraming()`.
 *
 * Each route file relays only paths under its own prefix, judged on the URL as
 * Next hands it over, with its dot segments resolved: see `isUnder()`.
 *
 * What the gateway sees is what the rewrite sent: every request header except
 * the hop-by-hop ones and the body's framing, `Host` set to the gateway, and
 * the caller's host in `X-Forwarded-Host`. One difference: when the caller sent no
 * `X-Forwarded-For` / `-Proto` / `-Port`, Next fills them in on a route
 * handler's request from the connection itself, and they travel on. Those
 * values come from the socket, not the caller, and the gateway trusts them
 * only from a peer listed in TRUSTED_PROXIES; behind Cloudflare the caller
 * always sends all three, so nothing changes there.
 *
 * A caller's own `X-Forwarded-For`, `X-Real-IP` and `CF-Connecting-IP` travel
 * unchanged, and this proxy adds no address to them: Next gives a route handler
 * no way to see the connection's address once the caller has sent
 * `X-Forwarded-For` (it fills that header in only when it is missing). So the
 * console cannot tell its edge's requests from anyone else's, and a gateway
 * that trusts the console trusts whatever can reach it: see "Behind the
 * console" in docs/developer/trusted-proxies-and-client-ips.md.
 */
import http from 'node:http';
import https from 'node:https';
import { Readable } from 'node:stream';
import type { ReadableStream as NodeReadableStream } from 'node:stream/web';

const BACKEND_INTERNAL_URL = (
  process.env.BUILT_BACKEND_INTERNAL_URL || 'http://backend:8080'
).replace(/\/+$/, '');

/**
 * How long a connection to the gateway may carry nothing, in either direction,
 * before it is closed. Not a deadline: a response that keeps arriving runs as
 * long as the gateway lets it. A working gateway is never this quiet -- it
 * answers or times out every request within REQUEST_TIMEOUT_SECONDS and sends
 * a stream a keepalive every 15s -- so this ends only a connection that has
 * stopped, which would otherwise hold its caller and its socket for good. An
 * hour, the rewrites' `proxyTimeout` in next.config.js, for the same reason:
 * the console has to outwait every limit the gateway sets, including one an
 * operator raised.
 */
export const UPSTREAM_IDLE_TIMEOUT_MS = 60 * 60 * 1000;

/** Connection-scoped headers (RFC 9110 §7.6.1), never relayed. */
const HOP_BY_HOP = new Set([
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

const agents = {
  'http:': new http.Agent({ keepAlive: true }),
  'https:': new https.Agent({ keepAlive: true }),
};

function connectionScoped(value: string | null): Set<string> {
  return new Set(
    (value || '')
      .split(',')
      .map((name) => name.trim().toLowerCase())
      .filter(Boolean),
  );
}

function upstreamHeaders(request: Request): Headers {
  const headers = new Headers();
  const scoped = connectionScoped(request.headers.get('connection'));
  request.headers.forEach((value, name) => {
    const lower = name.toLowerCase();
    if (HOP_BY_HOP.has(lower) || scoped.has(lower) || lower === 'host') return;
    // The body's framing is set from the body this proxy sends: bodyFraming().
    if (lower === 'content-length') return;
    headers.append(name, value);
  });
  // Next keeps the caller's Host on the request, and builds `request.url` from it.
  const host = request.headers.get('host') || new URL(request.url).host;
  if (host) headers.set('x-forwarded-host', host);
  return headers;
}

type BodyFraming =
  | { body: null; contentLength: null }
  | { body: ReadableStream<Uint8Array>; contentLength: string | null };

/**
 * The body the gateway is sent, and the length it is declared with (`null`:
 * sent chunked).
 *
 * The caller's framing cannot travel as it came. Next gives a GET or HEAD
 * handler no body whatever the caller declared, so a `Content-Length` relayed
 * with it would promise bytes that never come, and the gateway would take the
 * start of the next request on the connection -- someone else's -- as the rest
 * of this one. `Transfer-Encoding` is hop-by-hop and does not travel, and Node
 * frames a body it has no length for only for some methods: a DELETE's or an
 * OPTIONS' would go out raw, to be read as the next request.
 *
 * So a declared length travels only with the body it describes, and is held to
 * it (`strictContentLength`); any other body is sent chunked, and a request
 * without one declares nothing.
 */
function bodyFraming(request: Request): BodyFraming {
  if (!request.body) return { body: null, contentLength: null };
  const declared = request.headers.get('content-length');
  // Node's parser refuses a request that declares both, or a malformed length;
  // were one to arrive anyway, chunked is right whatever it said.
  const length =
    !request.headers.has('transfer-encoding') && declared !== null && /^\d+$/.test(declared)
      ? declared
      : null;
  return { body: request.body, contentLength: length };
}

/** Wait until `upstream` takes more, or fail once it has closed. */
function drained(upstream: http.ClientRequest): Promise<void> {
  if (upstream.destroyed) return Promise.reject(new Error('The gateway request was closed.'));
  return new Promise((resolve, reject) => {
    const settle = (error?: Error) => {
      upstream.off('drain', onDrain);
      upstream.off('close', onClose);
      if (error) reject(error);
      else resolve();
    };
    const onDrain = () => settle();
    const onClose = () => settle(new Error('The gateway request was closed.'));
    upstream.on('drain', onDrain);
    upstream.on('close', onClose);
  });
}

/**
 * Write the caller's body to the gateway as it arrives, at the pace the gateway
 * reads it. Any failure -- the caller's body breaking off, the gateway going
 * away, a body that does not match its declared length -- destroys the gateway
 * request, so its connection is never reused. Not `pipe()`: under
 * `strictContentLength` a length mismatch throws from `end()`, which inside a
 * pipe would escape as an uncaught exception.
 */
async function sendBody(body: ReadableStream<Uint8Array>, upstream: http.ClientRequest) {
  const reader = body.getReader();
  try {
    for (let chunk = await reader.read(); !chunk.done; chunk = await reader.read()) {
      if (!upstream.write(chunk.value)) await drained(upstream);
    }
    upstream.end();
  } catch (error) {
    upstream.destroy(error instanceof Error ? error : new Error(String(error)));
    reader.cancel().catch(() => {});
  }
}

function gatewayUnreachable(): Response {
  return Response.json(
    { error: { message: 'The gateway is not reachable.', type: 'server_error', code: 502 } },
    { status: 502, headers: { 'cache-control': 'no-store' } },
  );
}

/** The gateway's connection carried nothing for the idle limit. */
class GatewaySilent extends Error {}

function gatewaySilent(): Response {
  return Response.json(
    { error: { message: 'The gateway stopped responding.', type: 'server_error', code: 504 } },
    { status: 504, headers: { 'cache-control': 'no-store' } },
  );
}

/** Statuses that carry no body, whatever the gateway's headers say. */
function hasNoBody(method: string, status: number): boolean {
  return method === 'HEAD' || status === 204 || status === 205 || status === 304;
}

/**
 * Whether `pathname` is `mount` or a path below it.
 *
 * A handler gets its URL with dot segments already resolved. Next picks the
 * handler by the path as it was sent -- `/v1/%2e%2e/openapi.json` goes to the
 * `/v1` handler -- but builds `request.url` with the URL parser, which reads
 * `%2e%2e` as `..`: that handler sees `/openapi.json`. Relayed as it stands,
 * every gateway path would be reachable through every handler, including the
 * ones no handler or rewrite publishes.
 */
function isUnder(pathname: string, mount: string): boolean {
  return pathname === mount || pathname.startsWith(`${mount}/`);
}

function notUnderMount(): Response {
  return Response.json(
    { error: { message: 'Not found.', type: 'invalid_request_error', code: 404 } },
    { status: 404, headers: { 'cache-control': 'no-store' } },
  );
}

/**
 * The handler a route file exports for every method: it relays the requests
 * Next routes to that file, as long as their path stays under `mount` (the
 * file's own URL prefix, `/v1` for `src/app/v1/[[...path]]/route.ts`).
 */
export function gatewayRoute(mount: string): (request: Request) => Promise<Response> {
  return (request) => proxyToGateway(request, mount);
}

/**
 * Relay `request` to the same path on the gateway, or answer 404 when that
 * path is not under `mount`. `idleTimeoutMs` is a parameter so a test need not
 * wait an hour.
 */
export function proxyToGateway(
  request: Request,
  mount: string,
  idleTimeoutMs: number = UPSTREAM_IDLE_TIMEOUT_MS,
): Promise<Response> {
  const incoming = new URL(request.url);
  if (!isUnder(incoming.pathname, mount)) return Promise.resolve(notUnderMount());
  const target = new URL(`${BACKEND_INTERNAL_URL}${incoming.pathname}${incoming.search}`);
  const protocol = target.protocol === 'https:' ? 'https:' : 'http:';
  const client = protocol === 'https:' ? https : http;

  return new Promise<Response>((resolve) => {
    let answered = false;
    const headerObject: Record<string, string | string[]> = {};
    upstreamHeaders(request).forEach((value, name) => {
      const values = headerObject[name];
      headerObject[name] = Array.isArray(values) ? [...values, value] : [value];
    });
    const framing = bodyFraming(request);
    if (framing.body && framing.contentLength !== null) {
      headerObject['content-length'] = framing.contentLength;
    } else if (framing.body) {
      headerObject['transfer-encoding'] = 'chunked';
    }

    const upstream = client.request(target, {
      method: request.method,
      headers: headerObject,
      agent: agents[protocol],
    });
    // Node implements this on OutgoingMessage, which ClientRequest extends;
    // @types/node declares it on ServerResponse alone. The length-mismatch test
    // in gatewayProxy.test.ts holds it to what it does here.
    (upstream as http.ClientRequest & { strictContentLength: boolean }).strictContentLength =
      framing.contentLength !== null;
    upstream.setTimeout(idleTimeoutMs, () => upstream.destroy(new GatewaySilent()));

    // The caller hung up: stop the gateway's work rather than let it finish
    // a generation nobody will read.
    const onAbort = () => upstream.destroy(new Error('The caller went away.'));
    if (request.signal.aborted) onAbort();
    else request.signal.addEventListener('abort', onAbort, { once: true });

    upstream.on('response', (res) => {
      answered = true;
      // Nothing will read the rest of a body the gateway answered without, and
      // the caller may never send it: see "A request does not outlive its
      // response" above.
      res.once('close', () => {
        if (!upstream.writableFinished) upstream.destroy();
      });
      const headers = new Headers();
      const scoped = connectionScoped(
        Array.isArray(res.headers.connection)
          ? res.headers.connection.join(',')
          : (res.headers.connection ?? null),
      );
      for (let i = 0; i < res.rawHeaders.length; i += 2) {
        const name = res.rawHeaders[i];
        const lower = name.toLowerCase();
        if (HOP_BY_HOP.has(lower) || scoped.has(lower)) continue;
        headers.append(name, res.rawHeaders[i + 1]);
      }
      const status = res.statusCode ?? 502;
      if (hasNoBody(request.method, status)) {
        res.resume();
        resolve(new Response(null, { status, statusText: res.statusMessage, headers }));
        return;
      }
      // Node ends a response that lost its connection with an error, not an
      // end, and `toWeb` passes that on as a stream error -- which is what
      // makes Next break the caller's response instead of finishing it.
      const body = Readable.toWeb(res) as NodeReadableStream<Uint8Array>;
      resolve(
        new Response(body as unknown as ReadableStream<Uint8Array>, {
          status,
          statusText: res.statusMessage,
          headers,
        }),
      );
    });

    upstream.on('error', (error) => {
      // After the response has started, the body stream reports the failure.
      if (answered) return;
      resolve(error instanceof GatewaySilent ? gatewaySilent() : gatewayUnreachable());
    });

    if (framing.body) void sendBody(framing.body, upstream);
    else upstream.end();
  });
}
