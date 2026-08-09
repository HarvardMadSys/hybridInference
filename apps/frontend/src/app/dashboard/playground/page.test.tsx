// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const fetchWithAuth = vi.fn();

vi.mock('@/components/features/auth/ProtectedRoute', () => ({
  ProtectedRoute: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { loading: false, user: { role: 'internal' } } }),
}));

vi.mock('@/components/providers/AuthProvider', () => ({ hasRole: () => true }));

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useBranding: () => ({ appName: 'FreeInference' }),
}));

vi.mock('@/lib/api/client', () => ({
  fetchWithAuth: (...args: unknown[]) => fetchWithAuth(...args),
  jsonOrThrow: (resp: { json: () => unknown }) => resp.json(),
}));

import PlaygroundPage from './page';

const MODELS = {
  ok: true,
  json: async () => ({
    models: [{ id: 'glm-4.6', name: 'GLM 4.6', provider: 'zai', providers: [] }],
  }),
};

/** A minimal stand-in for the streaming half of `fetch`. */
function sseResponse(frames: string[]) {
  const encoder = new TextEncoder();
  let i = 0;
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: async () =>
          i < frames.length
            ? { done: false, value: encoder.encode(frames[i++]) }
            : { done: true, value: undefined },
      }),
    },
  };
}

function routeFrame(extra: Record<string, unknown> = {}): string {
  return `data: ${JSON.stringify({
    choices: [],
    _playground_route: { provider: 'zai', endpoint_id: 'glm-4.6:zai-api', ...extra },
  })}\n\n`;
}

function contentFrame(content: string): string {
  return `data: ${JSON.stringify({ choices: [{ index: 0, delta: { content } }] })}\n\n`;
}

function mockStream(frames: string[]): void {
  fetchWithAuth.mockImplementation(async (_base: string, path: string) =>
    path === '/internal/playground/models' ? MODELS : sseResponse(frames),
  );
}

async function sendPrompt(): Promise<void> {
  const send = await screen.findByRole('button', { name: 'Send' });
  fireEvent.change(screen.getByPlaceholderText(/Type a message/), { target: { value: 'hi' } });
  await waitFor(() => expect(send).not.toBeDisabled());
  fireEvent.click(send);
}

describe('PlaygroundPage', () => {
  beforeEach(() => {
    fetchWithAuth.mockReset();
    // jsdom has no layout engine, so the chat pane's auto-scroll is a no-op.
    Element.prototype.scrollTo = vi.fn() as unknown as typeof Element.prototype.scrollTo;
  });

  afterEach(cleanup);

  it('renders assistant markdown as styled elements, not literal text', async () => {
    mockStream([contentFrame('- one\n- two'), 'data: [DONE]\n\n']);
    const { container } = render(<PlaygroundPage />);
    await sendPrompt();

    // The page used to lean on `prose`, which is a no-op here — the typography
    // plugin is not installed. The utilities have to be on the elements.
    await waitFor(() => expect(container.querySelector('ul')).toHaveClass('list-disc'));
    expect(container.querySelectorAll('li')).toHaveLength(2);
    expect(screen.queryByText('- one')).toBeNull();
  });

  it('shows which endpoint served the turn', async () => {
    mockStream([routeFrame({ host: 'api.z.ai' }), contentFrame('hi'), 'data: [DONE]\n\n']);
    render(<PlaygroundPage />);
    await sendPrompt();

    const badge = await screen.findByTitle(/endpoint: glm-4\.6:zai-api/);
    expect(badge).toHaveTextContent('glm-4.6:zai-api');
    expect(badge.getAttribute('title')).toContain('host: api.z.ai');
    expect(badge).not.toHaveClass('text-amber-400');
  });

  it('flags a fallback and names the attempt that failed', async () => {
    mockStream([
      routeFrame({ host: 'api.z.ai' }),
      routeFrame({
        provider: 'chutes',
        endpoint_id: 'glm-4.6:chutes-api',
        host: 'llm.chutes.ai',
        fallback: true,
        failed_attempts: [
          {
            provider: 'zai',
            endpoint_id: 'glm-4.6:zai-api',
            error_type: 'HTTPStatusError',
          },
        ],
      }),
      contentFrame('hi'),
      'data: [DONE]\n\n',
    ]);
    render(<PlaygroundPage />);
    await sendPrompt();

    // Last frame wins: the fallback is what actually served the response.
    const badge = await screen.findByTitle(/served by fallback/);
    expect(badge).toHaveTextContent('glm-4.6:chutes-api');
    expect(badge).toHaveClass('text-amber-400');
    expect(badge.getAttribute('title')).toContain('failed: glm-4.6:zai-api (HTTPStatusError)');
  });

  it('keeps the routing badge when the stream dies mid-turn', async () => {
    const encoder = new TextEncoder();
    let sent = false;
    fetchWithAuth.mockImplementation(async (_base: string, path: string) => {
      if (path === '/internal/playground/models') return MODELS;
      return {
        ok: true,
        body: {
          getReader: () => ({
            read: async () => {
              if (sent) throw new Error('network error');
              sent = true;
              return { done: false, value: encoder.encode(routeFrame()) };
            },
          }),
        },
      };
    });
    render(<PlaygroundPage />);
    await sendPrompt();

    // A router exception after the routing frame is exactly when knowing the
    // endpoint matters most, so the error must not wipe the badge.
    await screen.findByText(/Error: network error/);
    expect(screen.getByTitle(/endpoint: glm-4\.6:zai-api/)).toBeInTheDocument();
  });
});
