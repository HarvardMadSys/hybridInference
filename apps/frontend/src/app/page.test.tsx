// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import HomePage from './page';

let authState = {
  loading: false,
  isAuthenticated: false,
  user: null as { id: string; email: string; role: string; user_name?: string | null } | null,
};

const NOTICE = 'Requests are logged by this deployment for research purposes.';

let dataPolicyNotice = NOTICE;
let distributionId = 'legacy';

const branding = {
  exampleApiBase: 'http://localhost:13001',
  exampleApiKeyEnvVar: 'HYBRIDINFERENCE_API_KEY',
  exampleModel: 'example-chat',
  get dataPolicyNotice() {
    return dataPolicyNotice;
  },
};

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useBranding: () => branding,
  useSiteConfig: () => ({
    branding,
    distribution: { id: distributionId, release: '' },
    features: { publicSignup: false, rag: false, agents: false },
  }),
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: authState,
  }),
}));

vi.mock('@/components/providers/AuthProvider', () => ({
  hasRole: (userRole: string | undefined, required: string) => userRole === required,
}));

vi.mock('@/components/landing', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/landing')>();
  return {
    ...actual,
    CodeExample: () => <section aria-label="code example" />,
    Features: () => <section aria-label="features" />,
    Hero: () => <section aria-label="hero" />,
    HowItWorks: () => <section aria-label="how it works" />,
    UseCases: () => <section aria-label="use cases" />,
    Sponsors: () => <section aria-label="sponsors" />,
    Updates: () => <section aria-label="updates" />,
  };
});

vi.mock('@/components/ui/UpdatesBanner', () => ({
  UpdatesBanner: () => <section aria-label="updates banner" />,
}));

vi.mock('@/components/features/dashboard/DashboardView', () => ({
  DashboardView: () => <section aria-label="dashboard view">Dashboard</section>,
}));

describe('HomePage', () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  beforeEach(() => {
    authState = {
      loading: false,
      isAuthenticated: false,
      user: null,
    };
    dataPolicyNotice = NOTICE;
    distributionId = 'legacy';
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

  it('keeps the existing marketing homepage for a non-example distribution', () => {
    authState = {
      loading: false,
      isAuthenticated: true,
      user: {
        id: 'user-1',
        email: 'user@example.com',
        role: 'free',
        user_name: 'Test User',
      },
    };

    render(<HomePage />);

    [/hero/i, /features/i, /use cases/i, /how it works/i, /code example/i, /sponsors/i].forEach(
      (pattern) => {
        expect(screen.getByLabelText(pattern)).toBeInTheDocument();
      },
    );
    expect(screen.queryByLabelText(/dashboard view/i)).not.toBeInTheDocument();
    expect(screen.getByText(/service is provided without guarantee/i)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(NOTICE, 'i'))).toBeInTheDocument();
  });

  it('renders the developer home only for the example distribution', async () => {
    distributionId = 'example';
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) === '/health') {
          return new Response(JSON.stringify({ status: 'healthy' }), { status: 200 });
        }
        return new Response(JSON.stringify({ data: [] }), { status: 200 });
      }),
    );

    render(<HomePage />);

    expect(
      screen.getByRole('heading', { name: /your local gateway is ready/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Dashboard' })).toHaveAttribute('href', '/dashboard');
    expect(screen.getByRole('link', { name: 'Playground' })).toHaveAttribute(
      'href',
      '/dashboard/playground',
    );
    expect(screen.getByRole('link', { name: 'Admin Console' })).toHaveAttribute(
      'href',
      '/dashboard/admin',
    );
    expect(screen.queryByLabelText(/hero/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/frontier|research community|free to use/i)).not.toBeInTheDocument();
    screen.getAllByRole('link').forEach((link) => {
      expect(link.getAttribute('href')).toMatch(/^\//);
    });

    await waitFor(() => expect(screen.getByText('Healthy')).toBeInTheDocument());
  });

  it('loads the example gateway status and model list from same-origin endpoints', async () => {
    distributionId = 'example';
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === '/health') {
        return new Response(JSON.stringify({ status: 'degraded' }), { status: 200 });
      }
      if (String(input) === '/v1/models') {
        return new Response(
          JSON.stringify({ data: [{ id: 'example-chat' }, { id: 'local-embedding' }] }),
          { status: 200 },
        );
      }
      throw new Error(`Unexpected request: ${String(input)}`);
    });
    vi.stubGlobal('fetch', fetchMock);

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
    const curlCommand = screen.getByText(
      (_, element) =>
        element?.tagName === 'CODE' && Boolean(element.textContent?.startsWith('curl ')),
    );
    expect(curlCommand).toHaveTextContent('http://localhost:13001/v1/chat/completions');
    expect(curlCommand).toHaveTextContent('"model": "example-chat"');
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
