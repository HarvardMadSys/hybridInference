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
 * the gateway's work at once. It sets no deadline of its own -- the gateway
 * bounds every request (REQUEST_TIMEOUT_SECONDS, STREAM_REQUEST_TIMEOUT_SECONDS)
 * and streams send a keepalive every 15s.
 *
 * What the gateway sees is what the rewrite sent: every request header except
 * the hop-by-hop ones, `Host` set to the gateway, and the caller's host in
 * `X-Forwarded-Host`. One difference: when the caller sent no
 * `X-Forwarded-For` / `-Proto` / `-Port`, Next fills them in on a route
 * handler's request from the connection itself, and they travel on. Those
 * values come from the socket, not the caller, and the gateway trusts them
 * only from a peer listed in TRUSTED_PROXIES; behind Cloudflare the caller
 * always sends all three, so nothing changes there.
 */
import http from 'node:http';
import https from 'node:https';
import { Readable } from 'node:stream';
import type { ReadableStream as NodeReadableStream } from 'node:stream/web';

const BACKEND_INTERNAL_URL = (
  process.env.BUILT_BACKEND_INTERNAL_URL || 'http://backend:8080'
).replace(/\/+$/, '');

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
    headers.append(name, value);
  });
  // Next keeps the caller's Host on the request, and builds `request.url` from it.
  const host = request.headers.get('host') || new URL(request.url).host;
  if (host) headers.set('x-forwarded-host', host);
  return headers;
}

function gatewayUnreachable(): Response {
  return Response.json(
    { error: { message: 'The gateway is not reachable.', type: 'server_error', code: 502 } },
    { status: 502, headers: { 'cache-control': 'no-store' } },
  );
}

/** Statuses that carry no body, whatever the gateway's headers say. */
function hasNoBody(method: string, status: number): boolean {
  return method === 'HEAD' || status === 204 || status === 205 || status === 304;
}

/** Relay `request` to the same path on the gateway. */
export function proxyToGateway(request: Request): Promise<Response> {
  const incoming = new URL(request.url);
  const target = new URL(`${BACKEND_INTERNAL_URL}${incoming.pathname}${incoming.search}`);
  const protocol = target.protocol === 'https:' ? 'https:' : 'http:';
  const client = protocol === 'https:' ? https : http;

  return new Promise<Response>((resolve) => {
    let answered = false;
    const headerObject: Record<string, string[]> = {};
    upstreamHeaders(request).forEach((value, name) => {
      (headerObject[name] ||= []).push(value);
    });

    const upstream = client.request(target, {
      method: request.method,
      headers: headerObject,
      agent: agents[protocol],
    });

    // The caller hung up: stop the gateway's work rather than let it finish
    // a generation nobody will read.
    const onAbort = () => upstream.destroy(new Error('The caller went away.'));
    if (request.signal.aborted) onAbort();
    else request.signal.addEventListener('abort', onAbort, { once: true });

    upstream.on('response', (res) => {
      answered = true;
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

    upstream.on('error', () => {
      // After the response has started, the body stream reports the failure.
      if (!answered) resolve(gatewayUnreachable());
    });

    if (request.body) {
      Readable.fromWeb(request.body as unknown as NodeReadableStream<Uint8Array>)
        .on('error', (err) => upstream.destroy(err))
        .pipe(upstream);
    } else {
      upstream.end();
    }
  });
}
