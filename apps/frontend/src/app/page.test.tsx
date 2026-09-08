// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DeveloperHome } from '@/components/landing';
import HomePage from './page';

type AuthUser = {
  id: string;
  email: string;
  role: string;
  user_name?: string | null;
  is_admin: boolean;
};

type CatalogEntry = string | { id: string; supported_features?: string[] };

let authState = {
  loading: false,
  isAuthenticated: false,
  user: null as AuthUser | null,
};

const NOTICE = 'Requests are logged by this deployment for research purposes.';

let dataPolicyNotice = NOTICE;
let distributionId = 'legacy';
let publicSignup = true;
let exampleModel = 'example-chat';
let exampleApiBase = 'http://localhost:13001';
let exampleHidden = false;

// Hoisted: lib/api/updates reads config.apiBase while the module graph loads,
// before this file's own top-level bindings exist.
const env = vi.hoisted(() => ({ apiBase: '' }));

const branding = {
  exampleApiKeyEnvVar: 'HYBRIDINFERENCE_API_KEY',
  get exampleApiBase() {
    return exampleApiBase;
  },
  get exampleHidden() {
    return exampleHidden;
  },
  get exampleModel() {
    return exampleModel;
  },
  get dataPolicyNotice() {
    return dataPolicyNotice;
  },
};

vi.mock('@/config/env', () => ({
  config: {
    get apiBase() {
      return env.apiBase;
    },
  },
}));

// The authenticated client adds the session token; the request itself still
// goes through the stubbed global fetch so one gateway stub serves both paths.
vi.mock('@/lib/api/client', () => ({
  fetchWithAuth: (apiBase: string, input: string, init: RequestInit = {}) =>
    fetch(`${apiBase}${input}`, { ...init, headers: { Authorization: 'Bearer session' } }),
}));

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useBranding: () => branding,
  useSiteConfig: () => ({
    branding,
    distribution: { id: distributionId, release: '' },
    features: { publicSignup, rag: false, agents: false },
  }),
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: authState,
  }),
}));

vi.mock('@/components/providers/AuthProvider', () => {
  const rank: Record<string, number> = { free: 0, pro: 1, internal: 2, admin: 3 };
  return {
    hasRole: (userRole: string | undefined, required: string) =>
      (rank[userRole ?? 'free'] ?? 0) >= (rank[required] ?? Number.POSITIVE_INFINITY),
  };
});

vi.mock('@/components/landing', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/landing')>();
  return {
    ...actual,
    Sponsors: () => <section aria-label="sponsors" />,
    Updates: () => <section aria-label="updates" />,
  };
});

vi.mock('@/components/ui/UpdatesBanner', () => ({
  UpdatesBanner: () => <section aria-label="updates banner" />,
}));

const FREE_USER: AuthUser = {
  id: 'user-1',
  email: 'user@example.com',
  role: 'free',
  is_admin: false,
};
const ADMIN_USER: AuthUser = {
  id: 'admin-1',
  email: 'admin@local.dev',
  role: 'admin',
  is_admin: true,
};

function signIn(user: AuthUser): void {
  authState = { loading: false, isAuthenticated: true, user };
}

function stubGateway(
  health: { status: string } | Error = { status: 'healthy' },
  publicCatalog: CatalogEntry[] = [],
  accountCatalog: CatalogEntry[] = publicCatalog,
) {
  const toData = (entries: CatalogEntry[]) =>
    entries.map((entry) => (typeof entry === 'string' ? { id: entry } : entry));
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (health instanceof Error) throw health;
    const url = String(input);
    if (url.endsWith('/health')) {
      return new Response(JSON.stringify(health), { status: 200 });
    }
    if (url.endsWith('/v1/models')) {
      return new Response(JSON.stringify({ data: toData(publicCatalog) }), { status: 200 });
    }
    if (url.endsWith('/user/models')) {
      return new Response(JSON.stringify({ data: toData(accountCatalog) }), { status: 200 });
    }
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function curlCommand(): HTMLElement {
  return screen.getByText(
    (_, element) =>
      element?.tagName === 'CODE' && Boolean(element.textContent?.startsWith('curl ')),
  );
}

function queryCurlCommand(): HTMLElement | null {
  return screen.queryByText(
    (_, element) =>
      element?.tagName === 'CODE' && Boolean(element.textContent?.startsWith('curl ')),
  );
}

function requestedPaths(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls.map((call) => String(call[0]));
}

describe('HomePage', () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  beforeEach(() => {
    authState = { loading: false, isAuthenticated: false, user: null };
    dataPolicyNotice = NOTICE;
    distributionId = 'legacy';
    publicSignup = true;
    exampleModel = 'example-chat';
    exampleApiBase = 'http://localhost:13001';
    exampleHidden = false;
    env.apiBase = '';
    stubGateway();
  });

  it('renders the developer home for every deployment, with no marketing copy', async () => {
    render(<HomePage />);

    expect(screen.getByRole('heading', { name: /your gateway is running/i })).toBeInTheDocument();
    expect(screen.queryByText(/local example/i)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/frontier|research community|free to use|no credit card/i),
    ).not.toBeInTheDocument();
    [/updates banner/i, /^updates$/i, /sponsors/i].forEach((pattern) => {
      expect(screen.getByLabelText(pattern)).toBeInTheDocument();
    });
    screen.getAllByRole('link').forEach((link) => {
      expect(link.getAttribute('href')).toMatch(/^\//);
    });

    await waitFor(() => expect(screen.getByText('Healthy')).toBeInTheDocument());
  });

  it('labels the example distribution as the local example', () => {
    distributionId = 'example';

    render(<HomePage />);

    expect(screen.getByText('Local example')).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { name: /your local gateway is running/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/inspect the example deployment/i)).toBeInTheDocument();
  });

  it('offers sign-in and sign-up to anonymous visitors, sign-up only with public signup', () => {
    const { unmount } = render(<HomePage />);

    expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/login');
    expect(screen.getByRole('link', { name: 'Sign up' })).toHaveAttribute('href', '/signup');
    expect(screen.queryByRole('link', { name: 'Dashboard' })).not.toBeInTheDocument();
    unmount();

    publicSignup = false;
    render(<HomePage />);

    expect(screen.getByRole('link', { name: 'Sign in' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Sign up' })).not.toBeInTheDocument();
  });

  it('opens the console for signed-in users, scoped to what their role can reach', () => {
    signIn(FREE_USER);
    const { unmount } = render(<HomePage />);

    expect(screen.getByRole('link', { name: 'Dashboard' })).toHaveAttribute('href', '/dashboard');
    expect(screen.queryByRole('link', { name: 'Playground' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Admin Console' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Sign in' })).not.toBeInTheDocument();
    unmount();

    signIn(ADMIN_USER);
    render(<HomePage />);

    expect(screen.getByRole('link', { name: 'Dashboard' })).toHaveAttribute('href', '/dashboard');
    expect(screen.getByRole('link', { name: 'Playground' })).toHaveAttribute(
      'href',
      '/dashboard/playground',
    );
    expect(screen.getByRole('link', { name: 'Admin Console' })).toHaveAttribute(
      'href',
      '/dashboard/admin',
    );
  });

  it('loads gateway status and the public catalog through the console API base', async () => {
    const fetchMock = stubGateway({ status: 'degraded' }, ['example-chat', 'local-embedding']);

    render(<HomePage />);

    expect(screen.getByText('Checking…')).toBeInTheDocument();
    expect(screen.getByText('Loading…')).toBeInTheDocument();
    expect(screen.getByText('Public models')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText('Degraded')).toBeInTheDocument();
      expect(screen.getByText('example-chat, local-embedding')).toBeInTheDocument();
    });
    // An empty API base means same-origin requests, exactly like every other
    // console call.
    expect(fetchMock).toHaveBeenCalledWith(
      '/health',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/models',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(requestedPaths(fetchMock)).not.toContain('/user/models');
    expect(screen.getByText('http://localhost:13001')).toBeInTheDocument();
    expect(curlCommand()).toHaveTextContent('http://localhost:13001/v1/chat/completions');
    expect(curlCommand()).toHaveTextContent('"model": "example-chat"');
  });

  it('loads the account catalog for signed-in users', async () => {
    // /v1/models without credentials is evaluated as the anonymous role and
    // hides everything gated above it; the session's own list does not.
    signIn(FREE_USER);
    const fetchMock = stubGateway({ status: 'healthy' }, [], ['gated-chat']);

    render(<HomePage />);

    expect(screen.getByText('Your models')).toBeInTheDocument();
    // The id also appears in the quickstart's "Uses" line; check the cell.
    await waitFor(() =>
      expect(screen.getByText('gated-chat', { selector: 'dd' })).toBeInTheDocument(),
    );
    expect(requestedPaths(fetchMock)).toContain('/user/models');
    expect(requestedPaths(fetchMock)).not.toContain('/v1/models');
    expect(curlCommand()).toHaveTextContent('"model": "gated-chat"');
  });

  it('waits for the session to resolve before choosing a catalog', async () => {
    authState = { loading: true, isAuthenticated: false, user: null };
    const fetchMock = stubGateway({ status: 'healthy' }, ['public-chat']);

    render(<HomePage />);

    await waitFor(() => expect(screen.getByText('Healthy')).toBeInTheDocument());
    expect(requestedPaths(fetchMock)).not.toContain('/v1/models');
    expect(requestedPaths(fetchMock)).not.toContain('/user/models');
    expect(screen.getByText('Loading…')).toBeInTheDocument();
  });

  it('follows a console built against a separate API origin', async () => {
    // A deployment that serves its API from another host sets
    // NEXT_PUBLIC_API_BASE; same-origin paths would hit the console and
    // report a healthy gateway as unreachable.
    env.apiBase = 'https://api.example.test';
    const fetchMock = stubGateway({ status: 'healthy' }, ['example-chat']);

    render(<HomePage />);

    await waitFor(() => expect(screen.getByText('Healthy')).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith('https://api.example.test/health', expect.anything());
    expect(fetchMock).toHaveBeenCalledWith('https://api.example.test/v1/models', expect.anything());
  });

  it('prefers a published example base, then the API origin, then the page origin', async () => {
    // With no published base the command follows the API origin the console
    // is built against: a console built against a real API must never
    // advertise a localhost default, even after /site-config fell back to
    // build-time branding.
    exampleApiBase = '';
    env.apiBase = 'https://api.example.test';
    stubGateway({ status: 'healthy' }, ['example-chat']);
    const { unmount } = render(<HomePage />);

    await waitFor(() =>
      expect(curlCommand()).toHaveTextContent('https://api.example.test/v1/chat/completions'),
    );
    expect(screen.getByText('https://api.example.test')).toBeInTheDocument();
    unmount();

    // Same-origin console: the page's own origin proxies /v1. curl cannot
    // resolve a relative URL, so the command never renders with one.
    env.apiBase = '';
    stubGateway({ status: 'healthy' }, ['example-chat']);
    render(<HomePage />);

    await waitFor(() =>
      expect(curlCommand()).toHaveTextContent(`${window.location.origin}/v1/chat/completions`),
    );
    expect(queryCurlCommand()?.textContent).not.toContain("'/v1/chat/completions'");
  });

  it('honours a distribution that hides its example on purpose', async () => {
    exampleApiBase = '';
    exampleHidden = true;
    env.apiBase = 'https://api.example.test';
    stubGateway({ status: 'healthy' }, ['example-chat']);

    render(<HomePage />);

    await waitFor(() => expect(screen.getByText('example-chat')).toBeInTheDocument());
    expect(queryCurlCommand()).not.toBeInTheDocument();
    expect(screen.queryByText('Try the API')).not.toBeInTheDocument();
    expect(screen.getByText('Not published')).toBeInTheDocument();
  });

  it('points the curl example at a chat model the gateway actually serves', async () => {
    // The build-time default names a model a fresh clone does not serve, and
    // the catalog lists embedding models first: the command must stay
    // copy-paste runnable against /v1/chat/completions.
    exampleModel = 'llama-3.3-70b';
    stubGateway({ status: 'healthy' }, [
      { id: 'embed-local', supported_features: ['embeddings'] },
      { id: 'glm-local', supported_features: ['tools'] },
    ]);

    render(<HomePage />);

    expect(curlCommand()).toHaveTextContent('"model": "llama-3.3-70b"');
    await waitFor(() => expect(curlCommand()).toHaveTextContent('"model": "glm-local"'));
    expect(screen.getByText('glm-local', { selector: 'code' })).toBeInTheDocument();
    expect(screen.getByText('embed-local, glm-local')).toBeInTheDocument();
  });

  it('tells anonymous visitors to sign in when no chat model is public', async () => {
    stubGateway({ status: 'healthy' }, [{ id: 'embed-local', supported_features: ['embeddings'] }]);

    render(<HomePage />);

    await waitFor(() => expect(screen.getByText('No public chat models.')).toBeInTheDocument());
    expect(queryCurlCommand()).not.toBeInTheDocument();
    expect(screen.getByText(/sign in to see the models/i)).toBeInTheDocument();
    expect(screen.queryByText(/add a route/i)).not.toBeInTheDocument();
  });

  it('tells a signed-in user to ask the operator when their account has no chat model', async () => {
    signIn(FREE_USER);
    stubGateway({ status: 'healthy' }, ['public-chat'], []);

    render(<HomePage />);

    await waitFor(() =>
      expect(screen.getByText('No chat models are available to your account.')).toBeInTheDocument(),
    );
    expect(screen.getByText('None configured')).toBeInTheDocument();
    expect(screen.getByText(/ask the operator/i)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Admin Console' })).not.toBeInTheDocument();
  });

  it('sends admins to the Admin Console when nothing is configured yet', async () => {
    signIn(ADMIN_USER);
    stubGateway({ status: 'healthy' }, [], []);

    render(<HomePage />);

    await waitFor(() => expect(screen.getByText('No chat models yet.')).toBeInTheDocument());
    expect(screen.getByText('None configured')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'Admin Console' })).toHaveLength(2);
  });

  it('says the gateway needs attention when it cannot be reached', async () => {
    stubGateway(new Error('connection refused'));

    render(<HomePage />);

    await waitFor(() => {
      expect(screen.getByText('Unreachable')).toBeInTheDocument();
      expect(screen.getByText('Unavailable')).toBeInTheDocument();
    });
    expect(
      screen.getByRole('heading', { name: /your gateway needs attention/i }),
    ).toBeInTheDocument();
  });

  it('gives up on a gateway that accepts the request and never answers', async () => {
    const fetchMock = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () =>
            reject(new DOMException('The operation was aborted.', 'AbortError')),
          );
        }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(<DeveloperHome requestTimeoutMs={20} />);

    expect(screen.getByText('Checking…')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText('Unreachable')).toBeInTheDocument();
      expect(screen.getByText('Unavailable')).toBeInTheDocument();
    });
  });

  it('gives up on a gateway that answers the headers and then stalls the body', async () => {
    // The deadline covers the body read, not only the first byte.
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const stalled = new Promise<never>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () =>
          reject(new DOMException('The operation was aborted.', 'AbortError')),
        );
      });
      return { ok: true, status: 200, json: () => stalled } as unknown as Response;
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<DeveloperHome requestTimeoutMs={20} />);

    await waitFor(() => {
      expect(screen.getByText('Unreachable')).toBeInTheDocument();
      expect(screen.getByText('Unavailable')).toBeInTheDocument();
    });
  });

  it('shows the no-guarantee notice before the data-policy notice', () => {
    const { container } = render(<HomePage />);

    const warrantyNotice = screen.getByText(/service is provided without guarantee/i);
    const loggingNotice = screen.getByText(new RegExp(NOTICE, 'i'));

    expect(warrantyNotice).toBeInTheDocument();
    expect(loggingNotice).toBeInTheDocument();
    expect(warrantyNotice.compareDocumentPosition(loggingNotice)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(container).toHaveTextContent(/provided without guarantee/i);
  });

  it('states no data policy when the deployment has not declared one', () => {
    // A deployment that configures nothing must not inherit someone else's
    // claim about what happens to its users' prompts.
    dataPolicyNotice = '';

    render(<HomePage />);

    expect(screen.getByText(/service is provided without guarantee/i)).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(NOTICE, 'i'))).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /terms of service/i })).toBeInTheDocument();
  });
});
