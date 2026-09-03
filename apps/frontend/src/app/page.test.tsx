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

// Hoisted: lib/api/updates reads config.apiBase while the module graph loads,
// before this file's own top-level bindings exist.
const env = vi.hoisted(() => ({ apiBase: '' }));

const branding = {
  exampleApiKeyEnvVar: 'HYBRIDINFERENCE_API_KEY',
  get exampleApiBase() {
    return exampleApiBase;
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

function stubGateway(
  health: { status: string } | Error = { status: 'healthy' },
  catalog: CatalogEntry[] = [],
) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (health instanceof Error) throw health;
    const url = String(input);
    if (url.endsWith('/health')) {
      return new Response(JSON.stringify(health), { status: 200 });
    }
    if (url.endsWith('/v1/models')) {
      const data = catalog.map((entry) => (typeof entry === 'string' ? { id: entry } : entry));
      return new Response(JSON.stringify({ data }), { status: 200 });
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
    authState = {
      loading: false,
      isAuthenticated: true,
      user: { id: 'user-1', email: 'user@example.com', role: 'free', is_admin: false },
    };
    const { unmount } = render(<HomePage />);

    expect(screen.getByRole('link', { name: 'Dashboard' })).toHaveAttribute('href', '/dashboard');
    expect(screen.queryByRole('link', { name: 'Playground' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Admin Console' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Sign in' })).not.toBeInTheDocument();
    unmount();

    authState = {
      loading: false,
      isAuthenticated: true,
      user: { id: 'admin-1', email: 'admin@local.dev', role: 'admin', is_admin: true },
    };
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

  it('loads gateway status and the model list through the console API base', async () => {
    const fetchMock = stubGateway({ status: 'degraded' }, ['example-chat', 'local-embedding']);

    render(<HomePage />);

    expect(screen.getByText('Checking…')).toBeInTheDocument();
    expect(screen.getByText('Loading…')).toBeInTheDocument();
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
    expect(screen.getByText('http://localhost:13001')).toBeInTheDocument();
    expect(curlCommand()).toHaveTextContent('http://localhost:13001/v1/chat/completions');
    expect(curlCommand()).toHaveTextContent('"model": "example-chat"');
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

  it('builds the curl example on an absolute origin when the distribution publishes none', async () => {
    // curl cannot resolve a relative URL, so a hidden example base falls back
    // to the API origin the console is built against, else the page origin.
    exampleApiBase = '';
    stubGateway({ status: 'healthy' }, ['example-chat']);

    render(<HomePage />);

    await waitFor(() =>
      expect(curlCommand()).toHaveTextContent(`${window.location.origin}/v1/chat/completions`),
    );
    expect(screen.getByText(window.location.origin)).toBeInTheDocument();
    expect(queryCurlCommand()?.textContent).not.toContain("'/v1/chat/completions'");
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

  it('replaces the quickstart with a hint when no chat model is served', async () => {
    stubGateway({ status: 'healthy' }, [{ id: 'embed-local', supported_features: ['embeddings'] }]);

    render(<HomePage />);

    await waitFor(() => expect(screen.getByText('No chat models yet.')).toBeInTheDocument());
    expect(queryCurlCommand()).not.toBeInTheDocument();
    expect(screen.getByText(/ask the operator/i)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Admin Console' })).not.toBeInTheDocument();
  });

  it('sends admins to the Admin Console when nothing is configured yet', async () => {
    authState = {
      loading: false,
      isAuthenticated: true,
      user: { id: 'admin-1', email: 'admin@local.dev', role: 'admin', is_admin: true },
    };
    stubGateway({ status: 'healthy' }, []);

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
