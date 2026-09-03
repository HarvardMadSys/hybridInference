// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import HomePage from './page';

type AuthUser = {
  id: string;
  email: string;
  role: string;
  user_name?: string | null;
  is_admin: boolean;
};

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

const branding = {
  exampleApiBase: 'http://localhost:13001',
  exampleApiKeyEnvVar: 'HYBRIDINFERENCE_API_KEY',
  get exampleModel() {
    return exampleModel;
  },
  get dataPolicyNotice() {
    return dataPolicyNotice;
  },
};

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
  modelIds: string[] = [],
) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (health instanceof Error) throw health;
    if (String(input) === '/health') {
      return new Response(JSON.stringify(health), { status: 200 });
    }
    if (String(input) === '/v1/models') {
      return new Response(JSON.stringify({ data: modelIds.map((id) => ({ id })) }), {
        status: 200,
      });
    }
    throw new Error(`Unexpected request: ${String(input)}`);
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
    stubGateway();
  });

  it('renders the developer home for every deployment, with no marketing copy', async () => {
    render(<HomePage />);

    expect(screen.getByRole('heading', { name: /your gateway is ready/i })).toBeInTheDocument();
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
      screen.getByRole('heading', { name: /your local gateway is ready/i }),
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

  it('loads gateway status and the model list from same-origin endpoints', async () => {
    const fetchMock = stubGateway({ status: 'degraded' }, ['example-chat', 'local-embedding']);

    render(<HomePage />);

    expect(screen.getByText('Checking…')).toBeInTheDocument();
    expect(screen.getByText('Loading…')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText('Degraded')).toBeInTheDocument();
      expect(screen.getByText('example-chat, local-embedding')).toBeInTheDocument();
    });
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

  it('points the curl example at a model the gateway actually serves', async () => {
    // The build-time default names a model a fresh clone does not serve; the
    // command must stay copy-paste runnable against this deployment.
    exampleModel = 'llama-3.3-70b';
    stubGateway({ status: 'healthy' }, ['glm-local', 'embed-local']);

    render(<HomePage />);

    expect(curlCommand()).toHaveTextContent('"model": "llama-3.3-70b"');
    await waitFor(() => expect(curlCommand()).toHaveTextContent('"model": "glm-local"'));
    expect(screen.getByText('glm-local', { selector: 'code' })).toBeInTheDocument();
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
