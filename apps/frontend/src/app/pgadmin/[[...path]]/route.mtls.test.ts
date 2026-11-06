/**
 * Opt-in mTLS wiring for the pgAdmin proxy.
 *
 * These tests stop at the boundary a unit test can honestly observe: the
 * credential files are read, an Agent receives them, and the upstream fetch is
 * given that Agent. No TLS handshake is attempted; the Agent is a synthetic
 * stand-in, and every buffer below is invented.
 *
 * The env is stubbed and the route is imported afresh per test because the
 * non-mTLS upstream URL is captured when the module is first evaluated.
 */
import { constants as fsConstants } from 'node:fs';
import { mkdir, mkdtemp, rm, symlink, writeFile, type FileHandle } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { NextRequest } from 'next/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The prototype mounts one Secret at this directory and projects ca.crt,
// client.crt and client.key into it; kubelet exposes them through `..data`.
const PROJECTION_DIR = '/etc/pgadmin-client-tls';
const CA_PATH = `${PROJECTION_DIR}/ca.crt`;
const CERT_PATH = `${PROJECTION_DIR}/client.crt`;
const KEY_PATH = `${PROJECTION_DIR}/client.key`;

const ioMocks = vi.hoisted(() => ({
  readFile: vi.fn(),
  readlink: vi.fn(),
  open: vi.fn(),
  /** Virtual plain-file entries; real temp-dir paths are opened for real. */
  plainFiles: new Map<string, PlainEntry>(),
  agents: [] as Array<{
    options: unknown;
    closed: boolean;
    closeCalls: number;
    close: () => Promise<void>;
  }>,
}));

vi.mock('node:fs/promises', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:fs/promises')>();
  return {
    ...actual,
    readFile: ioMocks.readFile as unknown as typeof actual.readFile,
    readlink: ioMocks.readlink as unknown as typeof actual.readlink,
    open: ioMocks.open as unknown as typeof actual.open,
  };
});

vi.mock('undici', async (importOriginal) => {
  const actual = await importOriginal<typeof import('undici')>();

  class FakeAgent {
    readonly options: unknown;
    closed = false;
    closeCalls = 0;

    constructor(options: unknown) {
      this.options = options;
      ioMocks.agents.push(this);
    }

    close(): Promise<void> {
      this.closeCalls += 1;
      this.closed = true;
      return Promise.resolve();
    }
  }

  return { ...actual, Agent: FakeAgent as unknown as typeof actual.Agent };
});

const VERIFY_URL = '/internal/verify-admin';
const ADMIN_COOKIE = 'refresh_token=admin; pga4_session=abc';

type AgentOptions = {
  connect: {
    ca: Buffer;
    cert: Buffer;
    key: Buffer;
    rejectUnauthorized: boolean;
  };
};

type UpstreamInit = RequestInit & { dispatcher?: unknown };

async function loadRoute() {
  vi.resetModules();
  return import('./route');
}

function enableMtls(overrides: Record<string, string> = {}): void {
  const env: Record<string, string> = {
    BUILT_BACKEND_INTERNAL_URL: 'http://backend:8080',
    PGADMIN_INTERNAL_URL: 'https://pgadmin.internal',
    PGADMIN_MTLS_ENABLED: 'true',
    PGADMIN_MTLS_CA_FILE: CA_PATH,
    PGADMIN_MTLS_CERT_FILE: CERT_PATH,
    PGADMIN_MTLS_KEY_FILE: KEY_PATH,
    ...overrides,
  };
  for (const [name, value] of Object.entries(env)) vi.stubEnv(name, value);
}

function stubPlainEnvironment(): void {
  vi.stubEnv('BUILT_BACKEND_INTERNAL_URL', 'http://backend:8080');
  vi.stubEnv('PGADMIN_INTERNAL_URL', 'http://pgadmin:80');
  vi.stubEnv('PGADMIN_MTLS_ENABLED', 'false');
}

function request(path = '/pgadmin/', cookie?: string): NextRequest {
  const headers = new Headers({ host: 'console.example' });
  if (cookie) headers.set('cookie', cookie);
  return new NextRequest(`https://console.example${path}`, { method: 'GET', headers });
}

function stubFetch(
  handlers: {
    verify?: () => Response | Promise<Response>;
    upstream?: (url: string, init: UpstreamInit) => Response | Promise<Response>;
  } = {},
) {
  const verify = vi.fn(handlers.verify ?? (() => new Response(null, { status: 200 })));
  const upstream = vi.fn(
    handlers.upstream ??
      ((_url: string, _init: UpstreamInit) => new Response('pgadmin', { status: 200 })),
  );

  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init: UpstreamInit = {}) =>
      String(input).includes(VERIFY_URL) ? verify() : upstream(String(input), init),
    ),
  );

  return { verify, upstream };
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function enoent(path: string): NodeJS.ErrnoException {
  return Object.assign(new Error(`ENOENT: no such file or directory, open '${path}'`), {
    code: 'ENOENT',
  });
}

function eloop(path: string): NodeJS.ErrnoException {
  return Object.assign(new Error(`ELOOP: too many symbolic links encountered, open '${path}'`), {
    code: 'ELOOP',
  });
}

function einval(path: string): NodeJS.ErrnoException {
  return Object.assign(new Error(`EINVAL: invalid argument, readlink '${path}'`), {
    code: 'EINVAL',
  });
}

type PlainEntry =
  | { kind: 'file'; content: Buffer }
  | { kind: 'symlink' }
  | { kind: 'nonregular' }
  | { kind: 'error'; error: NodeJS.ErrnoException };

/** A stand-in for the subset of FileHandle this route uses. */
function fakeHandle(content: Buffer, regular = true): FileHandle {
  return {
    stat: async () => ({ isFile: () => regular }),
    readFile: async () => content,
    close: async () => {},
  } as unknown as FileHandle;
}

/**
 * Model the plain-file branch without a real filesystem. Paths that are not in
 * the virtual map are real temp-dir files opened by the actual implementation,
 * so the kernel enforces O_NOFOLLOW for the focused tests below.
 */
function installPlainOpen(): void {
  ioMocks.open.mockImplementation(async (path: string, flags: number) => {
    const entry = ioMocks.plainFiles.get(path);
    if (!entry) {
      const actual = await vi.importActual<typeof import('node:fs/promises')>('node:fs/promises');
      return actual.open(path, flags);
    }
    if ((flags & fsConstants.O_NOFOLLOW) !== fsConstants.O_NOFOLLOW) {
      throw new Error(`test double: ${path} was opened without O_NOFOLLOW`);
    }
    if (entry.kind === 'error') throw entry.error;
    if (entry.kind === 'symlink') throw eloop(path);
    if (entry.kind === 'nonregular') return fakeHandle(Buffer.alloc(0), false);
    return fakeHandle(entry.content);
  });
}

function setPlainFile(path: string, content: string): void {
  ioMocks.plainFiles.set(path, { kind: 'file', content: Buffer.from(content) });
}

const tempDirs: string[] = [];

async function makeTempDir(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), 'pgadmin-mtls-'));
  tempDirs.push(dir);
  return dir;
}

type CredentialGeneration = {
  ca: string;
  cert: string;
  key: string;
};

type ProjectedLink =
  | { kind: 'symlink'; target: string }
  | { kind: 'missing' }
  | { kind: 'regular' };

const FILENAME_TO_FIELD: Record<string, keyof CredentialGeneration> = {
  'ca.crt': 'ca',
  'client.crt': 'cert',
  'client.key': 'key',
};

function generation(label: string): CredentialGeneration {
  return {
    ca: `synthetic-ca-${label}`,
    cert: `synthetic-client-cert-${label}`,
    key: `synthetic-client-key-${label}`,
  };
}

/** The per-key links kubelet writes: each one points at its `..data` entry. */
function defaultProjectedLinks(): Record<keyof CredentialGeneration, ProjectedLink> {
  return {
    ca: { kind: 'symlink', target: '..data/ca.crt' },
    cert: { kind: 'symlink', target: '..data/client.crt' },
    key: { kind: 'symlink', target: '..data/client.key' },
  };
}

/**
 * Model one Kubernetes Secret projection. The configured paths are per-key
 * symlinks through `..data`, which names one generation directory. Reading a
 * configured path therefore follows whatever `..data` currently names, while
 * reading a `..2026_*` path is fixed to that generation. `swapAfterFirstRead`
 * mimics kubelet replacing `..data` between two independent reads. `links`
 * overrides individual per-key links so a removed or malformed one can be
 * modelled.
 */
function installProjection(
  generations: Record<string, CredentialGeneration>,
  options: {
    target: string;
    swapAfterFirstRead?: string;
    missing?: keyof CredentialGeneration;
    links?: Partial<Record<keyof CredentialGeneration, ProjectedLink>>;
  },
): { current: () => string; directReads: string[] } {
  let current = options.target;
  let hasRead = false;
  const directReads: string[] = [];
  const links = { ...defaultProjectedLinks(), ...options.links };

  ioMocks.readlink.mockImplementation(async (path: string) => {
    if (path === `${PROJECTION_DIR}/..data`) return current;

    // The route also inspects each configured path; the mocked filesystem has
    // to answer for those with the link kubelet would have created.
    const name = path.startsWith(`${PROJECTION_DIR}/`)
      ? path.slice(PROJECTION_DIR.length + 1)
      : undefined;
    const field = name ? FILENAME_TO_FIELD[name] : undefined;
    if (!field) throw enoent(path);

    const link = links[field];
    if (link.kind === 'missing') throw enoent(path);
    if (link.kind === 'regular') throw einval(path);
    return link.target;
  });

  ioMocks.readFile.mockImplementation(async (path: string) => {
    if (!path.startsWith(`${PROJECTION_DIR}/`)) {
      // Anything else belongs to a dependency, not this route; keep it working.
      const actual = await vi.importActual<typeof import('node:fs/promises')>('node:fs/promises');
      return actual.readFile(path);
    }

    const relative = path.slice(PROJECTION_DIR.length + 1);
    const parts = relative.split('/');
    const isDirect = parts.length === 1 && !parts[0].startsWith('..');
    if (isDirect) directReads.push(path);

    const filename = isDirect ? parts[0] : parts[1];
    const field = filename ? FILENAME_TO_FIELD[filename] : undefined;
    const material = generations[isDirect ? current : parts[0]];
    if (!field || !material) throw enoent(path);
    if (options.missing === field) throw enoent(path);

    const value = material[field];
    if (options.swapAfterFirstRead && !hasRead) {
      hasRead = true;
      current = options.swapAfterFirstRead;
    }
    return Buffer.from(value);
  });

  return { current: () => current, directReads };
}

/** Change the cert bytes so the next credential read yields a new fingerprint. */
function rotateCredentials(): void {
  setPlainFile(CA_PATH, 'synthetic-ca');
  setPlainFile(CERT_PATH, 'synthetic-cert-rotated');
  setPlainFile(KEY_PATH, 'synthetic-key');
}

beforeEach(() => {
  ioMocks.readFile.mockReset();
  ioMocks.readlink.mockReset();
  ioMocks.open.mockReset();
  ioMocks.plainFiles.clear();
  ioMocks.agents.length = 0;
  // The plain-file branch is what these paths model: no `..data` projection.
  setPlainFile(CA_PATH, 'synthetic-ca');
  setPlainFile(CERT_PATH, 'synthetic-cert');
  setPlainFile(KEY_PATH, 'synthetic-key');
  installPlainOpen();
  // Anything not modelled above is a dependency read; keep it working.
  ioMocks.readFile.mockImplementation(async (path: string) => {
    const actual = await vi.importActual<typeof import('node:fs/promises')>('node:fs/promises');
    return actual.readFile(path);
  });
  // The default is a plain directory, not a Kubernetes Secret projection.
  ioMocks.readlink.mockRejectedValue(enoent(`${PROJECTION_DIR}/..data`));
});

afterEach(async () => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  await Promise.all(tempDirs.splice(0).map((dir) => rm(dir, { recursive: true, force: true })));
});

describe('pgAdmin proxy — opt-in mTLS', () => {
  it('fails closed with 502 when the client certificate cannot be read', async () => {
    enableMtls();
    ioMocks.plainFiles.set(CERT_PATH, { kind: 'error', error: enoent(CERT_PATH) });
    const { upstream } = stubFetch();
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(response.status).toBe(502);
    expect(upstream).not.toHaveBeenCalled();
    expect(ioMocks.agents).toHaveLength(0);
  });

  it('fails closed with 502 when the upstream is not HTTPS', async () => {
    enableMtls({ PGADMIN_INTERNAL_URL: 'http://pgadmin:80' });
    const { upstream } = stubFetch();
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(response.status).toBe(502);
    expect(ioMocks.readFile).not.toHaveBeenCalled();
    expect(ioMocks.open).not.toHaveBeenCalled();
    expect(upstream).not.toHaveBeenCalled();
    expect(ioMocks.agents).toHaveLength(0);
  });

  it.each(['PGADMIN_MTLS_CA_FILE', 'PGADMIN_MTLS_CERT_FILE', 'PGADMIN_MTLS_KEY_FILE'] as const)(
    'fails closed with 502 when %s is missing',
    async (name) => {
      enableMtls({ [name]: '' });
      const { upstream } = stubFetch();
      const { GET } = await loadRoute();

      const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

      expect(response.status).toBe(502);
      expect(ioMocks.readFile).not.toHaveBeenCalled();
      expect(ioMocks.open).not.toHaveBeenCalled();
      expect(upstream).not.toHaveBeenCalled();
      expect(ioMocks.agents).toHaveLength(0);
    },
  );

  it('forwards an authenticated admin over HTTPS with a dedicated agent', async () => {
    enableMtls();
    const { upstream } = stubFetch({
      verify: () => new Response(null, { status: 200 }),
      upstream: () => new Response('pgadmin-ui', { status: 200 }),
    });
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/browser/js/utils.js?ver=8', ADMIN_COOKIE));

    expect(response.status).toBe(200);
    await expect(response.text()).resolves.toBe('pgadmin-ui');
    expect(upstream).toHaveBeenCalledOnce();

    const [url, init] = upstream.mock.calls[0] as [string, UpstreamInit];
    expect(url).toBe('https://pgadmin.internal/pgadmin/browser/js/utils.js?ver=8');
    expect(init).toHaveProperty('dispatcher');

    expect(ioMocks.agents).toHaveLength(1);
    expect(init.dispatcher).toBe(ioMocks.agents[0]);

    const options = ioMocks.agents[0].options as AgentOptions;
    expect(Object.keys(options.connect).sort()).toEqual([
      'ca',
      'cert',
      'key',
      'rejectUnauthorized',
    ]);
    expect(Buffer.isBuffer(options.connect.ca)).toBe(true);
    expect(Buffer.isBuffer(options.connect.cert)).toBe(true);
    expect(Buffer.isBuffer(options.connect.key)).toBe(true);
    expect(options.connect.ca.toString()).toBe('synthetic-ca');
    expect(options.connect.cert.toString()).toBe('synthetic-cert');
    expect(options.connect.key.toString()).toBe('synthetic-key');
    expect(options.connect.rejectUnauthorized).toBe(true);

    const opened = ioMocks.open.mock.calls.map(([path]) => path);
    expect(opened).toEqual([CA_PATH, CERT_PATH, KEY_PATH]);
    for (const [, flags] of ioMocks.open.mock.calls as Array<[string, number]>) {
      expect(flags & fsConstants.O_NOFOLLOW).toBe(fsConstants.O_NOFOLLOW);
    }
    expect(ioMocks.readFile).not.toHaveBeenCalled();
  });

  it('uses one credential generation when ..data swaps between projected reads', async () => {
    enableMtls();
    const projected = installProjection(
      {
        '..2026_old': generation('old'),
        '..2026_new': generation('new'),
      },
      { target: '..2026_old', swapAfterFirstRead: '..2026_new' },
    );
    const { upstream } = stubFetch({
      upstream: () => new Response('pgadmin-ui', { status: 200 }),
    });
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(response.status).toBe(200);
    await expect(response.text()).resolves.toBe('pgadmin-ui');
    expect(upstream).toHaveBeenCalledOnce();
    expect(ioMocks.agents).toHaveLength(1);

    // The first read starts on the old generation; the swap happens before the
    // other two. All three must still come from the old one, never a mixture.
    expect(projected.current()).toBe('..2026_new');
    const options = ioMocks.agents[0].options as AgentOptions;
    expect(options.connect.ca.toString()).toBe('synthetic-ca-old');
    expect(options.connect.cert.toString()).toBe('synthetic-client-cert-old');
    expect(options.connect.key.toString()).toBe('synthetic-client-key-old');
    expect(options.connect.rejectUnauthorized).toBe(true);

    // The next request observes the new link and gets a complete new set.
    const rotated = await GET(request('/pgadmin/next', ADMIN_COOKIE));

    expect(rotated.status).toBe(200);
    expect(ioMocks.agents).toHaveLength(2);
    const nextOptions = ioMocks.agents[1].options as AgentOptions;
    expect(nextOptions.connect.ca.toString()).toBe('synthetic-ca-new');
    expect(nextOptions.connect.cert.toString()).toBe('synthetic-client-cert-new');
    expect(nextOptions.connect.key.toString()).toBe('synthetic-client-key-new');
    expect(nextOptions.connect.rejectUnauthorized).toBe(true);
  });

  it('fails closed when a file in the projected generation cannot be read', async () => {
    enableMtls();
    installProjection(
      {
        '..2026_old': generation('old'),
        '..2026_new': generation('new'),
      },
      { target: '..2026_old', missing: 'key' },
    );
    const { upstream } = stubFetch();
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(response.status).toBe(502);
    await expect(response.text()).resolves.toBe('pgAdmin is not reachable.');
    expect(upstream).not.toHaveBeenCalled();
    expect(ioMocks.agents).toHaveLength(0);
  });

  it('fails closed when the configured client key link is missing from the projection', async () => {
    enableMtls();
    // The generation still contains client.key. Only the configured per-key
    // link is gone, which is what an unavailable-credential harness removes.
    installProjection(
      { '..2026_current': generation('current') },
      { target: '..2026_current', links: { key: { kind: 'missing' } } },
    );
    const { upstream } = stubFetch();
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(response.status).toBe(502);
    await expect(response.text()).resolves.toBe('pgAdmin is not reachable.');
    expect(upstream).not.toHaveBeenCalled();
    // Validation happens before the pinned generation is read, so the surviving
    // client.key target cannot stand in for the configured path.
    expect(ioMocks.readFile).not.toHaveBeenCalled();
    expect(ioMocks.agents).toHaveLength(0);
  });

  it.each<[string, ProjectedLink]>([
    ['a different key', { kind: 'symlink', target: '..data/other.key' }],
    ['another generation', { kind: 'symlink', target: '..2026_current/client.key' }],
    ['an absolute path', { kind: 'symlink', target: `${PROJECTION_DIR}/..data/client.key` }],
    ['a regular file', { kind: 'regular' }],
  ])('fails closed when the client key link is %s', async (_label, link) => {
    enableMtls();
    installProjection(
      { '..2026_current': generation('current') },
      { target: '..2026_current', links: { key: link } },
    );
    const { upstream } = stubFetch();
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(response.status).toBe(502);
    await expect(response.text()).resolves.toBe('pgAdmin is not reachable.');
    expect(upstream).not.toHaveBeenCalled();
    expect(ioMocks.readFile).not.toHaveBeenCalled();
    expect(ioMocks.agents).toHaveLength(0);
  });

  it('refuses a ..data link that leaves the credential directory', async () => {
    enableMtls();
    installProjection(
      {
        '../elsewhere': generation('outside'),
      },
      { target: '../elsewhere' },
    );
    const { upstream } = stubFetch();
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(response.status).toBe(502);
    expect(upstream).not.toHaveBeenCalled();
    expect(ioMocks.agents).toHaveLength(0);
    expect(ioMocks.readFile).not.toHaveBeenCalled();
    expect(ioMocks.open).not.toHaveBeenCalled();
  });

  it('fails closed when ..data cannot be inspected for a reason other than absence', async () => {
    enableMtls();
    ioMocks.readlink.mockRejectedValue(
      Object.assign(new Error('EACCES: permission denied'), { code: 'EACCES' }),
    );
    const { upstream } = stubFetch();
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(response.status).toBe(502);
    expect(upstream).not.toHaveBeenCalled();
    expect(ioMocks.readFile).not.toHaveBeenCalled();
    expect(ioMocks.open).not.toHaveBeenCalled();
    expect(ioMocks.agents).toHaveLength(0);
  });

  it('reads ordinary files when there is no projection', async () => {
    const dir = await makeTempDir();
    const caPath = join(dir, 'ca.crt');
    const certPath = join(dir, 'client.crt');
    const keyPath = join(dir, 'client.key');
    await writeFile(caPath, 'plain-ca');
    await writeFile(certPath, 'plain-cert');
    await writeFile(keyPath, 'plain-key');
    enableMtls({
      PGADMIN_MTLS_CA_FILE: caPath,
      PGADMIN_MTLS_CERT_FILE: certPath,
      PGADMIN_MTLS_KEY_FILE: keyPath,
    });
    const { upstream } = stubFetch({
      upstream: () => new Response('pgadmin-ui', { status: 200 }),
    });
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(response.status).toBe(200);
    await expect(response.text()).resolves.toBe('pgadmin-ui');
    expect(upstream).toHaveBeenCalledOnce();
    expect(ioMocks.agents).toHaveLength(1);

    const options = ioMocks.agents[0].options as AgentOptions;
    expect(options.connect.ca.toString()).toBe('plain-ca');
    expect(options.connect.cert.toString()).toBe('plain-cert');
    expect(options.connect.key.toString()).toBe('plain-key');
    expect(options.connect.rejectUnauthorized).toBe(true);

    // Never a path-based read: each file is opened without following links and
    // checked through the resulting descriptor.
    expect(ioMocks.readFile).not.toHaveBeenCalled();
    const opened = ioMocks.open.mock.calls as Array<[string, number]>;
    expect(opened.map(([path]) => path)).toEqual([caPath, certPath, keyPath]);
    for (const [, flags] of opened) {
      expect(flags & fsConstants.O_NOFOLLOW).toBe(fsConstants.O_NOFOLLOW);
      expect(flags & fsConstants.O_NONBLOCK).toBe(fsConstants.O_NONBLOCK);
    }
  });

  it('applies the no-symlink rule when the paths use different directories', async () => {
    const caDir = await makeTempDir();
    const certDir = await makeTempDir();
    const keyDir = await makeTempDir();
    const caPath = join(caDir, 'ca.crt');
    const certPath = join(certDir, 'client.crt');
    const keyPath = join(keyDir, 'client.key');
    await writeFile(caPath, 'split-ca');
    await writeFile(certPath, 'split-cert');
    await writeFile(keyPath, 'split-key');
    enableMtls({
      PGADMIN_MTLS_CA_FILE: caPath,
      PGADMIN_MTLS_CERT_FILE: certPath,
      PGADMIN_MTLS_KEY_FILE: keyPath,
    });
    const { upstream } = stubFetch();
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(response.status).toBe(200);
    expect(upstream).toHaveBeenCalledOnce();
    expect(ioMocks.agents).toHaveLength(1);
    const options = ioMocks.agents[0].options as AgentOptions;
    expect(options.connect.ca.toString()).toBe('split-ca');
    expect(options.connect.cert.toString()).toBe('split-cert');
    expect(options.connect.key.toString()).toBe('split-key');

    // The different-directory branch takes the same restricted read.
    expect(ioMocks.readlink).not.toHaveBeenCalled();
    expect(ioMocks.readFile).not.toHaveBeenCalled();
    const opened = ioMocks.open.mock.calls as Array<[string, number]>;
    expect(opened.map(([path]) => path)).toEqual([caPath, certPath, keyPath]);
    for (const [, flags] of opened) {
      expect(flags & fsConstants.O_NOFOLLOW).toBe(fsConstants.O_NOFOLLOW);
    }
  });

  it('refuses a configured credential path that is a symlink', async () => {
    const dir = await makeTempDir();
    const target = join(dir, 'replacement.pem');
    const caPath = join(dir, 'ca.crt');
    const certPath = join(dir, 'client.crt');
    const keyPath = join(dir, 'client.key');
    await writeFile(target, 'symlinked-ca');
    await symlink(target, caPath);
    await writeFile(certPath, 'plain-cert');
    await writeFile(keyPath, 'plain-key');
    enableMtls({
      PGADMIN_MTLS_CA_FILE: caPath,
      PGADMIN_MTLS_CERT_FILE: certPath,
      PGADMIN_MTLS_KEY_FILE: keyPath,
    });
    const { upstream } = stubFetch();
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    // The kernel refuses to follow the link, so the target is never read as
    // credentials and no Agent is built.
    expect(response.status).toBe(502);
    await expect(response.text()).resolves.toBe('pgAdmin is not reachable.');
    expect(upstream).not.toHaveBeenCalled();
    expect(ioMocks.agents).toHaveLength(0);
    expect(ioMocks.readFile).not.toHaveBeenCalled();
  });

  it('refuses a configured credential path that is not a regular file', async () => {
    const dir = await makeTempDir();
    const caPath = join(dir, 'ca.crt');
    const certPath = join(dir, 'client.crt');
    const keyPath = join(dir, 'client.key');
    await mkdir(caPath);
    await writeFile(certPath, 'plain-cert');
    await writeFile(keyPath, 'plain-key');
    enableMtls({
      PGADMIN_MTLS_CA_FILE: caPath,
      PGADMIN_MTLS_CERT_FILE: certPath,
      PGADMIN_MTLS_KEY_FILE: keyPath,
    });
    const { upstream } = stubFetch();
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(response.status).toBe(502);
    expect(upstream).not.toHaveBeenCalled();
    expect(ioMocks.agents).toHaveLength(0);
    expect(ioMocks.readFile).not.toHaveBeenCalled();
  });

  it('refuses a path that becomes a symlink after an earlier credential was opened', async () => {
    const oldDir = await makeTempDir();
    const newDir = await makeTempDir();
    const caPath = join(oldDir, 'ca.crt');
    const certPath = join(oldDir, 'client.crt');
    const keyPath = join(oldDir, 'client.key');
    await writeFile(caPath, 'generation-old-ca');
    await writeFile(certPath, 'generation-old-cert');
    await writeFile(keyPath, 'generation-old-key');
    await writeFile(join(newDir, 'client.crt'), 'generation-new-cert');

    const actual = await vi.importActual<typeof import('node:fs/promises')>('node:fs/promises');
    let caOpened = false;
    let swapped = false;
    ioMocks.open.mockImplementation(async (path: string, flags: number) => {
      if (path === caPath) caOpened = true;
      if (path === certPath && caOpened && !swapped) {
        // kubelet replacing a projected name with a new-generation symlink
        // after the CA was already opened but before the certificate is.
        swapped = true;
        const staged = `${certPath}.kubelet`;
        await actual.symlink(join(newDir, 'client.crt'), staged);
        await actual.rename(staged, certPath);
      }
      return actual.open(path, flags);
    });
    enableMtls({
      PGADMIN_MTLS_CA_FILE: caPath,
      PGADMIN_MTLS_CERT_FILE: certPath,
      PGADMIN_MTLS_KEY_FILE: keyPath,
    });
    const { upstream } = stubFetch();
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(swapped).toBe(true);
    expect(response.status).toBe(502);
    expect(upstream).not.toHaveBeenCalled();
    // No mixture of the old certificate with any other generation is built.
    expect(ioMocks.agents).toHaveLength(0);
    expect(ioMocks.readFile).not.toHaveBeenCalled();
  });

  it('reads through the opened descriptor when the path is replaced before the read', async () => {
    const dir = await makeTempDir();
    const caPath = join(dir, 'ca.crt');
    const certPath = join(dir, 'client.crt');
    const keyPath = join(dir, 'client.key');
    await writeFile(caPath, 'plain-ca-old');
    await writeFile(certPath, 'plain-cert');
    await writeFile(keyPath, 'plain-key');

    const actual = await vi.importActual<typeof import('node:fs/promises')>('node:fs/promises');
    let replaced = false;
    ioMocks.open.mockImplementation(async (path: string, flags: number) => {
      const handle = await actual.open(path, flags);
      if (path === caPath && !replaced) {
        // Replace the path with a different file after its descriptor exists.
        replaced = true;
        const staged = `${caPath}.replacement`;
        await actual.writeFile(staged, 'plain-ca-new');
        await actual.rename(staged, caPath);
      }
      return handle;
    });
    enableMtls({
      PGADMIN_MTLS_CA_FILE: caPath,
      PGADMIN_MTLS_CERT_FILE: certPath,
      PGADMIN_MTLS_KEY_FILE: keyPath,
    });
    const { upstream } = stubFetch();
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(response.status).toBe(200);
    expect(upstream).toHaveBeenCalledOnce();
    expect(ioMocks.agents).toHaveLength(1);
    const options = ioMocks.agents[0].options as AgentOptions;
    // The descriptor, not the path, decided which bytes were used.
    expect(options.connect.ca.toString()).toBe('plain-ca-old');
    expect(options.connect.cert.toString()).toBe('plain-cert');
    expect(options.connect.key.toString()).toBe('plain-key');
    expect(replaced).toBe(true);
    await expect(actual.readFile(caPath, 'utf8')).resolves.toBe('plain-ca-new');
    expect(ioMocks.readFile).not.toHaveBeenCalled();
  });

  it('keeps a retired agent open until the request dispatching on it receives headers', async () => {
    enableMtls();
    const oldUpstream = deferred<Response>();
    const oldDispatched = deferred<void>();
    const { upstream } = stubFetch({
      upstream: (_url, init) => {
        if (init.dispatcher === ioMocks.agents[0]) {
          oldDispatched.resolve(undefined);
          return oldUpstream.promise;
        }
        return new Response('new-response', { status: 200 });
      },
    });
    const { GET } = await loadRoute();

    const oldRequest = GET(request('/pgadmin/old', ADMIN_COOKIE));
    await oldDispatched.promise;

    const oldAgent = ioMocks.agents[0];
    expect(oldAgent.closed).toBe(false);
    expect(upstream).toHaveBeenCalledTimes(1);

    // Rotate while the old request is still waiting for its response headers.
    // The newer request swaps the cache and retires the old agent.
    rotateCredentials();
    const newResponse = await GET(request('/pgadmin/new', ADMIN_COOKIE));

    expect(newResponse.status).toBe(200);
    await expect(newResponse.text()).resolves.toBe('new-response');
    expect(ioMocks.agents).toHaveLength(2);
    const newAgent = ioMocks.agents[1];
    expect(newAgent).not.toBe(oldAgent);
    // Retired, but the old request still holds a dispatch lease.
    expect(oldAgent.closed).toBe(false);
    expect(oldAgent.closeCalls).toBe(0);

    oldUpstream.resolve(new Response('old-response', { status: 200 }));
    const oldResponse = await oldRequest;

    expect(oldResponse.status).toBe(200);
    await expect(oldResponse.text()).resolves.toBe('old-response');
    // Headers arrived, the lease was released in finally, and only then was
    // the retired agent closed.
    expect(oldAgent.closed).toBe(true);
    expect(oldAgent.closeCalls).toBe(1);
  });

  it('queues a concurrent credential read so both requests share the cached agent', async () => {
    enableMtls();

    // Each batch is the three files one request opens. Opens are deferred so
    // the test can observe the queue instead of racing the scheduler.
    const reads: Array<{ path: string; gate: ReturnType<typeof deferred<Buffer>> }> = [];
    const oldReadsStarted = deferred<void>();
    const newReadsStarted = deferred<void>();
    let outstanding = 0;

    ioMocks.open.mockImplementation((path: string) => {
      if (path !== CA_PATH && path !== CERT_PATH && path !== KEY_PATH) {
        // Anything else belongs to a dependency, not this route; keep it working.
        return vi
          .importActual<typeof import('node:fs/promises')>('node:fs/promises')
          .then((actual) => actual.open(path));
      }

      const gate = deferred<Buffer>();
      reads.push({ path, gate });
      outstanding += 1;
      if (reads.length === 3) oldReadsStarted.resolve(undefined);
      if (reads.length === 6) newReadsStarted.resolve(undefined);

      return gate.promise.then((content) => {
        outstanding -= 1;
        return fakeHandle(content);
      });
    });

    const releaseReads = (start: number) => {
      for (const { path, gate } of reads.slice(start, start + 3)) {
        if (path === CA_PATH) gate.resolve(Buffer.from('synthetic-ca'));
        else if (path === CERT_PATH) gate.resolve(Buffer.from('synthetic-cert'));
        else gate.resolve(Buffer.from('synthetic-key'));
      }
    };

    const { upstream } = stubFetch({
      upstream: (url, init) => {
        const dispatcher = init.dispatcher as { closed?: boolean } | undefined;
        // A cached agent must still be open when a later request dispatches.
        if (dispatcher?.closed) throw new Error('pgAdmin mTLS dispatcher is already closed');
        return new Response(url.endsWith('/pgadmin/old') ? 'old-response' : 'new-response', {
          status: 200,
        });
      },
    });
    const { GET } = await loadRoute();

    const oldRequest = GET(request('/pgadmin/old', ADMIN_COOKIE));
    const newRequest = GET(request('/pgadmin/new', ADMIN_COOKIE));

    // Only the old request's three opens may start. The newer request observes
    // the same files but waits behind the old read and cache swap.
    await oldReadsStarted.promise;
    expect(ioMocks.open).toHaveBeenCalledTimes(3);
    expect(outstanding).toBe(3);
    expect(ioMocks.agents).toHaveLength(0);

    // Let the old request finish reading and publish the one cached agent. It
    // also finishes dispatching before the newer read is allowed to complete,
    // which is the window where a cached agent must not be closed.
    releaseReads(0);
    const oldResponse = await oldRequest;
    await newReadsStarted.promise;

    expect(oldResponse.status).toBe(200);
    await expect(oldResponse.text()).resolves.toBe('old-response');
    expect(ioMocks.open).toHaveBeenCalledTimes(6);
    expect(outstanding).toBe(3);
    expect(ioMocks.agents).toHaveLength(1);
    const agent = ioMocks.agents[0];
    expect(agent.closed).toBe(false);

    // The queued read now finishes with the same material, so the new request
    // leases the cached agent rather than creating a second one.
    releaseReads(3);
    const newResponse = await newRequest;

    expect(newResponse.status).toBe(200);
    await expect(newResponse.text()).resolves.toBe('new-response');
    expect(upstream).toHaveBeenCalledTimes(2);
    expect(ioMocks.agents).toHaveLength(1);
    expect(agent.closed).toBe(false);
    expect(agent.closeCalls).toBe(0);
    for (const [, init] of upstream.mock.calls) expect(init.dispatcher).toBe(agent);
  });

  it('releases its lease when the upstream fetch rejects', async () => {
    enableMtls();
    stubFetch({
      upstream: (url) => {
        if (url.endsWith('/pgadmin/first')) throw new Error('connection refused');
        return new Response('pgadmin', { status: 200 });
      },
    });
    const { GET } = await loadRoute();

    const failed = await GET(request('/pgadmin/first', ADMIN_COOKIE));

    expect(failed.status).toBe(502);
    await expect(failed.text()).resolves.toBe('pgAdmin is not reachable.');
    expect(ioMocks.agents).toHaveLength(1);
    const oldAgent = ioMocks.agents[0];
    expect(oldAgent.closed).toBe(false);
    expect(oldAgent.closeCalls).toBe(0);

    // Rotating retires the agent. If the rejected fetch had leaked its lease,
    // the retirement would never reach zero and close() would not run.
    rotateCredentials();
    const response = await GET(request('/pgadmin/second', ADMIN_COOKIE));

    expect(response.status).toBe(200);
    expect(ioMocks.agents).toHaveLength(2);
    expect(oldAgent.closed).toBe(true);
    expect(oldAgent.closeCalls).toBe(1);
  });

  it('leaves the plain-HTTP path untouched when mTLS is not opted in', async () => {
    stubPlainEnvironment();
    const { upstream } = stubFetch({
      verify: () => new Response(null, { status: 200 }),
    });
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(response.status).toBe(200);
    expect(ioMocks.readFile).not.toHaveBeenCalled();
    expect(ioMocks.agents).toHaveLength(0);

    const [url, init] = upstream.mock.calls[0] as [string, UpstreamInit];
    expect(url).toBe('http://pgadmin/pgadmin/');
    expect(init.dispatcher).toBeUndefined();
  });

  it('keeps the plain-HTTP path when mTLS is explicitly disabled', async () => {
    enableMtls({
      PGADMIN_INTERNAL_URL: 'http://pgadmin:80',
      PGADMIN_MTLS_ENABLED: 'false',
    });
    const { verify, upstream } = stubFetch({
      verify: () => new Response(null, { status: 200 }),
    });
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(response.status).toBe(200);
    expect(verify).toHaveBeenCalledOnce();
    expect(ioMocks.readFile).not.toHaveBeenCalled();
    expect(ioMocks.agents).toHaveLength(0);

    const [url, init] = upstream.mock.calls[0] as [string, UpstreamInit];
    expect(url).toBe('http://pgadmin/pgadmin/');
    expect(init.dispatcher).toBeUndefined();
  });

  it('keeps the plain-HTTP path when the mTLS setting is absent', async () => {
    stubPlainEnvironment();
    vi.stubEnv('PGADMIN_MTLS_ENABLED', undefined);
    const { upstream } = stubFetch({
      verify: () => new Response(null, { status: 200 }),
    });
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

    expect(response.status).toBe(200);
    expect(ioMocks.readFile).not.toHaveBeenCalled();
    expect(ioMocks.agents).toHaveLength(0);

    const [url, init] = upstream.mock.calls[0] as [string, UpstreamInit];
    expect(url).toBe('http://pgadmin/pgadmin/');
    expect(init.dispatcher).toBeUndefined();
  });

  it.each(['', 'True', '1'])(
    'fails closed with 502 for the invalid mTLS value %j',
    async (value) => {
      enableMtls({ PGADMIN_MTLS_ENABLED: value });
      const { verify, upstream } = stubFetch();
      const { GET } = await loadRoute();

      const response = await GET(request('/pgadmin/', ADMIN_COOKIE));

      expect(response.status).toBe(502);
      await expect(response.text()).resolves.toBe('pgAdmin is not reachable.');
      expect(verify).toHaveBeenCalledOnce();
      expect(upstream).not.toHaveBeenCalled();
      expect(ioMocks.readFile).not.toHaveBeenCalled();
      expect(ioMocks.agents).toHaveLength(0);
    },
  );

  it('never reads credentials or contacts upstream for an anonymous caller', async () => {
    enableMtls();
    const { verify, upstream } = stubFetch();
    const { GET } = await loadRoute();

    const response = await GET(request('/pgadmin/'));

    expect(response.status).toBe(302);
    expect(response.headers.get('location')).toBe('/login');
    expect(verify).not.toHaveBeenCalled();
    expect(upstream).not.toHaveBeenCalled();
    expect(ioMocks.readFile).not.toHaveBeenCalled();
    expect(ioMocks.agents).toHaveLength(0);
  });
});
