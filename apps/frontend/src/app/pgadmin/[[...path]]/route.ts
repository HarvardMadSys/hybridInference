/**
 * Admin-gated reverse proxy for pgAdmin.
 *
 * Why this lives in the console rather than in a web server out front: the
 * Cloudflare tunnel routes public traffic straight to this container, so this
 * app sees it first, and the console's own rewrite table (next.config.js) is
 * what actually routes `/v1/`, `/auth/`, `/health` and friends on to FastAPI.
 * The gate used to be an Nginx `/pgadmin/` block with `auth_request`; once
 * nothing public went through Nginx that block stopped running — clicking
 * pgAdmin in the dashboard returned the console's own 404 page — and host nginx
 * was purged from both deployments on 2026-09-13.
 *
 * A rewrite alone cannot replace it: rewrites cannot authenticate. Whether
 * pgAdmin also asks for a login depends on the host — `SERVER_MODE` defaults
 * to False in the pgadmin service, and a host that sets it True gains a second
 * gate. This code cannot tell which, so it assumes it is the only thing in
 * front of a database console: it fails closed on every unexpected condition
 * rather than falling through.
 */
import { createHash } from 'node:crypto';
import { constants, open, readFile, readlink } from 'node:fs/promises';
import { basename, dirname, join, resolve } from 'node:path';
import { NextResponse, type NextRequest } from 'next/server';
import { Agent, type Dispatcher } from 'undici';
import { withoutConsoleSession } from '@/lib/utils/consoleSessionCookie';

const BACKEND_INTERNAL_URL = process.env.BUILT_BACKEND_INTERNAL_URL || 'http://backend:8080';
const PGADMIN_INTERNAL_URL = process.env.PGADMIN_INTERNAL_URL || 'http://pgadmin:80';

type PgAdminRequestInit = RequestInit & {
  dispatcher?: Dispatcher;
  duplex?: 'half';
};

type AgentLease = {
  dispatcher: Agent;
  release: () => void;
};

type PgAdminUpstream = {
  baseUrl: string;
  lease?: AgentLease;
};

type MtlsConfig = {
  baseUrl: string;
  caPath: string;
  certPath: string;
  keyPath: string;
};

type MtlsCredentials = {
  ca: Buffer;
  cert: Buffer;
  key: Buffer;
};

type CachedMtlsAgent = {
  fingerprint: string;
  agent: Agent;
  /** Requests that hold this agent and have not yet released it. */
  activeLeases: number;
  /** Replaced by a newer fingerprint; never handed to another request. */
  retired: boolean;
  /** close() has been called; guards against closing twice. */
  closing: boolean;
};

let cachedMtlsAgent: CachedMtlsAgent | null = null;

/**
 * Credential reads and the cache swap they perform are serialized in call
 * order. Without this, a slow read of older material could finish after a
 * newer read and publish the older agent over it. The upstream fetches
 * themselves are deliberately not serialized.
 */
let credentialQueue: Promise<void> = Promise.resolve();

/**
 * Must match `SCRIPT_NAME` on the pgadmin service in
 * deploy/docker/docker-compose.yml — pgAdmin builds its own asset and redirect
 * URLs from it, so the two have to agree or every page loads without styles.
 */
const PGADMIN_SCRIPT_NAME = '/pgadmin';

/**
 * The generation link kubelet maintains beside the per-key links in a projected
 * Secret volume. Each key is a symlink to `..data/<key>`.
 */
const PROJECTED_DATA_LINK = '..data';

/** Budget for the admin check. Exceeding it denies, it does not admit. */
const VERIFY_TIMEOUT_MS = 5_000;

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

function requestHeaders(request: NextRequest): Headers {
  const headers = new Headers();
  request.headers.forEach((value, name) => {
    if (!UNFORWARDABLE_HEADERS.has(name.toLowerCase())) headers.set(name, value);
  });

  // pgAdmin keeps its own session cookie; only the console's is dropped.
  const cookie = withoutConsoleSession(request.headers.get('cookie'));
  if (cookie) {
    headers.set('cookie', cookie);
  } else {
    headers.delete('cookie');
  }

  headers.set('x-forwarded-proto', request.nextUrl.protocol.replace(':', ''));
  const host = request.headers.get('host');
  if (host) headers.set('x-forwarded-host', host);

  // Next fills `x-forwarded-port` in with its own listening port whenever the
  // incoming request carries none, and pgAdmin's ProxyFix trusts that header
  // (PROXY_X_PORT_COUNT is 1 by default) while ignoring x-forwarded-host
  // (PROXY_X_HOST_COUNT is 0). It therefore grafts the console's port onto the
  // Host of this fetch and believes it is serving from `pgadmin:3001` — an
  // address that resolves nowhere in a browser, and one that reaches the
  // client the moment Werkzeug builds an absolute URL. Dropping it leaves the
  // Host we dial as pgAdmin's only clue, which is the one foldUpstreamRedirect
  // recognises.
  headers.delete('x-forwarded-port');

  return headers;
}

/**
 * Fold a redirect that points at the upstream itself back to a bare path.
 *
 * pgAdmin's own redirects are relative, but Werkzeug builds an absolute one of
 * its own when a request arrives without the trailing slash a route requires —
 * from the Host header it saw, which here is the container name and resolves
 * nowhere in a browser. Derived from the configured upstream rather than a
 * literal so it still holds when PGADMIN_INTERNAL_URL is overridden. Returns
 * null to leave the value alone.
 *
 * Matched on hostname rather than origin: the port pgAdmin names is not
 * necessarily the one we dial, because ProxyFix rebuilds its idea of the Host
 * from any x-forwarded-port it is given. Whatever port is on it, a URL naming
 * the upstream's own hostname is unreachable from a browser, so folding it is
 * always the right answer.
 */
function foldUpstreamRedirect(location: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(location, PGADMIN_INTERNAL_URL);
  } catch {
    return null;
  }
  if (parsed.hostname !== new URL(PGADMIN_INTERNAL_URL).hostname) return null;
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
    // A bare path, not an absolute URL. Behind the tunnel this app sees its own
    // bind address as the Host, so `new URL('/login', request.nextUrl)` renders
    // as https://0.0.0.0:3001/login and strands the browser — observed on
    // staging. A relative Location is resolved against the address the client
    // actually used. NextResponse.redirect() only accepts absolute URLs, hence
    // the header written by hand; middleware.ts does not need this because Next
    // relativizes middleware redirects on its own.
    return new NextResponse(null, { status: 302, headers: { location: '/login' } });
  }
  return new NextResponse('Admin access required.', {
    status: 403,
    headers: { 'content-type': 'text/plain; charset=utf-8' },
  });
}

function serializeCredentialRead<T>(task: () => Promise<T>): Promise<T> {
  const run = credentialQueue.then(task);
  credentialQueue = run.then(
    () => undefined,
    () => undefined,
  );
  return run;
}

function closeWhenIdle(entry: CachedMtlsAgent): void {
  if (entry.closing || entry.activeLeases > 0) return;
  entry.closing = true;
  try {
    // close() drains in-flight requests; destroy() would cut their streams.
    // Reaching here means no request can still dispatch on this agent.
    void entry.agent.close().catch(() => {});
  } catch {
    // Closing is best-effort; a retired agent is never reissued.
  }
}

function leaseAgent(entry: CachedMtlsAgent): AgentLease {
  if (entry.retired || entry.closing) {
    throw new Error('pgAdmin mTLS agent is no longer available.');
  }

  // Synchronous, before the caller can dispatch: a concurrent rotation sees
  // this lease and defers closing the agent until it is released.
  entry.activeLeases += 1;
  let released = false;

  return {
    dispatcher: entry.agent,
    release: () => {
      if (released) return;
      released = true;
      entry.activeLeases -= 1;
      if (entry.retired) closeWhenIdle(entry);
    },
  };
}

/**
 * Read and validate the mTLS settings without touching the filesystem.
 *
 * Done at request time, before the read is queued, so each request uses the
 * configuration it observed when it arrived.
 */
function readMtlsConfig(): MtlsConfig {
  const baseUrl = process.env.PGADMIN_INTERNAL_URL;
  const caPath = process.env.PGADMIN_MTLS_CA_FILE;
  const certPath = process.env.PGADMIN_MTLS_CERT_FILE;
  const keyPath = process.env.PGADMIN_MTLS_KEY_FILE;

  if (!baseUrl || !caPath || !certPath || !keyPath) {
    throw new Error('Invalid pgAdmin mTLS configuration.');
  }

  let parsed: URL;
  try {
    parsed = new URL(baseUrl);
  } catch {
    throw new Error('Invalid pgAdmin mTLS configuration.');
  }
  if (parsed.protocol !== 'https:') {
    throw new Error('Invalid pgAdmin mTLS configuration.');
  }

  return { baseUrl, caPath, certPath, keyPath };
}

/**
 * Read one credential file, refusing to follow it if it is a symlink.
 *
 * The path is opened with O_NOFOLLOW and the descriptor itself is checked to be
 * a regular file before any bytes are read, so both decisions describe the
 * object that was actually opened rather than a path looked up separately. A
 * path that has become a symlink makes open() fail with ELOOP; a FIFO,
 * directory or device is rejected by the descriptor check. O_NONBLOCK only
 * keeps open() from blocking on a FIFO before that check can reject it.
 */
async function readRegularCredential(path: string): Promise<Buffer> {
  const flags = constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK;
  const handle = await open(path, flags);
  try {
    const stats = await handle.stat();
    if (!stats.isFile()) {
      throw new Error('Invalid pgAdmin mTLS credential file.');
    }
    return await handle.readFile();
  } finally {
    await handle.close();
  }
}

async function readIndependentCredentials(config: MtlsConfig): Promise<MtlsCredentials> {
  const [ca, cert, key] = await Promise.all([
    readRegularCredential(config.caPath),
    readRegularCredential(config.certPath),
    readRegularCredential(config.keyPath),
  ]);
  return { ca, cert, key };
}

/**
 * Require one configured path to be the per-key symlink kubelet writes.
 *
 * A projected Secret is not just a `..data` link: every key is its own symlink
 * whose target is exactly `..data/<key>`. Reading the generation directory
 * directly without checking those links would keep serving a key whose
 * configured symlink had been removed, even though that path is what a client
 * or an operator actually configures. `readlink` observes the link itself, so
 * a missing link (ENOENT), a non-symlink (EINVAL) and any target that is not
 * the exact sibling link are all refused.
 */
async function requireProjectedKeyLink(path: string): Promise<void> {
  let target: string;
  try {
    target = await readlink(path);
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code === 'ENOENT' || code === 'ENOTDIR' || code === 'EINVAL') {
      throw new Error('Invalid pgAdmin mTLS credential projection.');
    }
    throw error;
  }

  // Kubelet writes a relative link to `..data/<basename>`. An absolute path,
  // another generation, a nested path or a different name is not a shape this
  // reader supports, and following it could select material outside the
  // generation pinned below.
  if (target !== `${PROJECTED_DATA_LINK}/${basename(path)}`) {
    throw new Error('Invalid pgAdmin mTLS credential projection.');
  }
}

/**
 * Read one consistent generation of Kubernetes-projected mTLS material.
 *
 * A Kubernetes Secret volume does not put ordinary files at the configured
 * paths: each name is a symlink through `..data` to a timestamped generation
 * directory, and kubelet rotates by atomically replacing that one link. Reading
 * the three configured paths independently can therefore follow the old link
 * for the CA and the new one for the certificate or key. Resolving `..data`
 * once, then reading every file from the directory it named, keeps material
 * from different generations out of the same Agent. Any failure is left to the
 * caller, which fails closed.
 *
 * Before reading the pinned generation, every configured path must still be
 * the per-key symlink `..data/<basename>`. The generation directory is read
 * directly, so without this check a removed or malformed configured link would
 * be invisible while its target file survived the rotation. This gates a new
 * snapshot, not an instantaneous revocation: a request that already read the
 * files, or one that passed this check before a link was removed, still
 * finishes with the material it observed.
 *
 * Other paths are opened as regular files with O_NOFOLLOW: this prevents a
 * projection created during fallback from being followed as independent
 * symlinks. Independent regular files cannot provide an atomic multi-file
 * snapshot; their operator must not replace or edit them during a read.
 */
async function readMtlsCredentials(config: MtlsConfig): Promise<MtlsCredentials> {
  const { caPath, certPath, keyPath } = config;
  const directory = resolve(dirname(caPath));

  if (resolve(dirname(certPath)) !== directory || resolve(dirname(keyPath)) !== directory) {
    return readIndependentCredentials(config);
  }

  let dataTarget: string;
  try {
    dataTarget = await readlink(join(directory, PROJECTED_DATA_LINK));
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    // ENOENT/ENOTDIR means no projection. EINVAL means `..data` is not a
    // symlink, so these are ordinary files as well. Every other failure is
    // unexpected and must not quietly fall back to independent reads.
    if (code === 'ENOENT' || code === 'ENOTDIR' || code === 'EINVAL') {
      return readIndependentCredentials(config);
    }
    throw error;
  }

  // A kubelet target is a sibling generation directory. Refuse anything that
  // would take the read outside the configured projection directory.
  const generation = resolve(directory, dataTarget);
  if (dirname(generation) !== directory) {
    throw new Error('Invalid pgAdmin mTLS credential projection.');
  }

  // Validate every configured name before reading any generation file. A
  // missing key link must fail the request even though `..data` still names a
  // generation containing `client.key`.
  await Promise.all([caPath, certPath, keyPath].map(requireProjectedKeyLink));

  // `..data` can be replaced again after the readlink, so never read through
  // the configured symlinks: read from the concrete generation named above.
  const [ca, cert, key] = await Promise.all(
    [caPath, certPath, keyPath].map((path) => readFile(join(generation, basename(path)))),
  );
  return { ca, cert, key };
}

/**
 * Resolve the pgAdmin upstream and, when opted in, its mTLS dispatcher.
 *
 * This is only called after the admin check and never at startup. With the
 * opt-in absent it returns the existing URL and no lease. With it present,
 * every credential file is read afresh so changed material is noticed, then
 * an Agent is cached by the hash of the URL and all three files. Reads and
 * cache swaps run one at a time so they cannot publish out of order, and an
 * Agent is leased before it is returned so a later rotation defers closing it
 * until every request using it has finished dispatching.
 */
async function resolveMtlsUpstream(config: MtlsConfig): Promise<PgAdminUpstream> {
  const { baseUrl } = config;
  const { ca, cert, key } = await readMtlsCredentials(config);

  const fingerprint = createHash('sha256')
    .update(baseUrl)
    .update('\0')
    .update(ca)
    .update('\0')
    .update(cert)
    .update('\0')
    .update(key)
    .digest('hex');

  const current = cachedMtlsAgent;
  if (current?.fingerprint === fingerprint) {
    return { baseUrl, lease: leaseAgent(current) };
  }

  const agent = new Agent({
    connect: {
      ca,
      cert,
      key,
      // Keep Node's default hostname verification and reject untrusted chains.
      rejectUnauthorized: true,
    },
  });

  const replacement: CachedMtlsAgent = {
    fingerprint,
    agent,
    activeLeases: 0,
    retired: false,
    closing: false,
  };
  cachedMtlsAgent = replacement;

  if (current) {
    // Retire only after the replacement is published. If a request is still
    // dispatching on the old agent, closeWhenIdle waits for its last lease.
    current.retired = true;
    closeWhenIdle(current);
  }

  return { baseUrl, lease: leaseAgent(replacement) };
}

async function pgAdminUpstream(): Promise<PgAdminUpstream> {
  const mtlsEnabled = process.env.PGADMIN_MTLS_ENABLED;
  if (mtlsEnabled === undefined || mtlsEnabled === 'false') {
    return { baseUrl: PGADMIN_INTERNAL_URL };
  }
  if (mtlsEnabled !== 'true') {
    // Fail closed for any other spelling or value. The value is deliberately
    // omitted from the error because it may contain deployed configuration.
    throw new Error('Invalid pgAdmin mTLS configuration.');
  }

  // Enqueue synchronously so concurrent reads start in arrival order.
  const config = readMtlsConfig();
  return serializeCredentialRead(() => resolveMtlsUpstream(config));
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
    const { baseUrl, lease } = await pgAdminUpstream();
    try {
      const init: PgAdminRequestInit = {
        method: request.method,
        headers: requestHeaders(request),
        body: hasBody ? request.body : undefined,
        // Streaming a request body requires half duplex; the option is not yet
        // in the DOM RequestInit type that Next ships.
        ...(hasBody ? { duplex: 'half' as const } : {}),
        redirect: 'manual',
        cache: 'no-store',
      };
      if (lease) init.dispatcher = lease.dispatcher;

      upstream = await fetch(new URL(pathname + search, baseUrl), init);
    } finally {
      // Headers have arrived (or the fetch threw); undici's close() drains the
      // response stream that has already been handed back to us.
      lease?.release();
    }
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
