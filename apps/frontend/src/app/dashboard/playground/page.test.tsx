// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
  // Like the real one: a non-2xx body becomes an Error carrying its message.
  jsonOrThrow: async (resp: { ok?: boolean; json: () => Promise<unknown> }) => {
    if (resp.ok === false) {
      const body = (await resp.json()) as { error?: { message?: string } };
      throw new Error(body.error?.message ?? 'Request failed');
    }
    return resp.json();
  },
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

async function sendPrompt(text = 'hi'): Promise<void> {
  const send = await screen.findByRole('button', { name: 'Send' });
  fireEvent.change(screen.getByPlaceholderText(/Type a message/), { target: { value: text } });
  await waitFor(() => expect(send).not.toBeDisabled());
  fireEvent.click(send);
}

function frame(body: Record<string, unknown>): string {
  return `data: ${JSON.stringify(body)}\n\n`;
}

function errorResponse(message: string) {
  return { ok: false, status: 502, json: async () => ({ error: { message } }) };
}

/** Chat responses served in order; the models request always succeeds. */
function mockChats(...responses: unknown[]): void {
  fetchWithAuth.mockImplementation(async (_base: string, path: string) =>
    path === '/internal/playground/models' ? MODELS : responses.shift(),
  );
}

/** The `messages` each chat request carried, in order. */
function sentHistories(): unknown[] {
  return fetchWithAuth.mock.calls
    .filter(([, path]) => path === '/internal/playground/chat')
    .map(([, , init]) => JSON.parse((init as { body: string }).body).messages);
}

/** A stream the test feeds frame by frame. */
function controlledStream() {
  const encoder = new TextEncoder();
  const queue: string[] = [];
  let ended = false;
  let wake: (() => void) | null = null;
  let parkedReject: ((reason: unknown) => void) | null = null;
  const read = () =>
    new Promise<{ done: boolean; value?: Uint8Array }>((resolve, reject) => {
      const next = () => {
        if (queue.length) resolve({ done: false, value: encoder.encode(queue.shift()) });
        else if (ended) resolve({ done: true, value: undefined });
        else {
          // Park exactly the way a real reader does; `failRead` rejects the
          // parked read the way a real one rejects when its signal aborts.
          parkedReject = reject;
          wake = next;
        }
      };
      next();
    });
  const poke = () => {
    const w = wake;
    wake = null;
    parkedReject = null;
    w?.();
  };
  return {
    response: { ok: true, body: { getReader: () => ({ read }) } },
    push: (f: string) => {
      queue.push(f);
      poke();
    },
    end: () => {
      ended = true;
      poke();
    },
    failRead: () => {
      const reject = parkedReject;
      poke();
      reject?.(new DOMException('The operation was aborted.', 'AbortError'));
    },
  };
}

describe('PlaygroundPage', () => {
  beforeEach(() => {
    fetchWithAuth.mockReset();
    // jsdom has no layout engine, so the chat pane's auto-scroll is a no-op.
    Element.prototype.scrollTo = vi.fn() as unknown as typeof Element.prototype.scrollTo;
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(cleanup);

  it('preserves a pinned provider when reselecting the current model', async () => {
    fetchWithAuth.mockResolvedValue({
      ok: true,
      json: async () => ({
        models: [
          {
            id: 'glm-4.6',
            name: 'GLM 4.6',
            provider: 'zai',
            providers: [
              { id: 'glm-4.6:zai-api', name: 'Z.AI' },
              { id: 'glm-4.6:chutes-api', name: 'Chutes' },
            ],
          },
        ],
      }),
    });
    render(<PlaygroundPage />);
    const picker = await screen.findByRole('button', { name: 'Model: GLM 4.6' });
    fireEvent.change(screen.getByRole('combobox'), {
      target: { value: 'glm-4.6:chutes-api' },
    });

    fireEvent.click(picker);
    fireEvent.click(screen.getByRole('option', { name: /GLM 4.6, glm-4.6/ }));

    expect(screen.getByRole('combobox')).toHaveValue('glm-4.6:chutes-api');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(picker).toHaveFocus();
  });

  it('resets the provider when changing models while preserving each session draft and settings', async () => {
    fetchWithAuth.mockResolvedValue({
      ok: true,
      json: async () => ({
        models: [
          {
            id: 'glm-4.6',
            name: 'GLM 4.6',
            provider: 'zai',
            providers: [
              { id: 'glm-4.6:zai-api', name: 'Z.AI' },
              { id: 'glm-4.6:chutes-api', name: 'Chutes' },
            ],
          },
          {
            id: 'deepseek-v4',
            name: 'DeepSeek V4',
            provider: 'deepseek',
            providers: [
              { id: 'deepseek-v4:direct-api', name: 'DeepSeek' },
              { id: 'deepseek-v4:chutes-api', name: 'Chutes' },
            ],
          },
        ],
      }),
    });
    render(<PlaygroundPage />);
    await screen.findByRole('button', { name: 'Model: GLM 4.6' });
    const firstSession = screen.getByRole('button', { name: /New session No messages -- glm-4.6/ });
    fireEvent.change(screen.getByRole('combobox'), {
      target: { value: 'glm-4.6:chutes-api' },
    });
    fireEvent.change(screen.getByPlaceholderText(/Type a message/), {
      target: { value: 'First session draft' },
    });
    fireEvent.change(screen.getByPlaceholderText('You are a research assistant...'), {
      target: { value: 'First session instructions' },
    });

    fireEvent.click(screen.getByRole('button', { name: '+ New' }));
    fireEvent.click(screen.getByRole('button', { name: 'Model: GLM 4.6' }));
    fireEvent.click(screen.getByRole('option', { name: /DeepSeek V4, deepseek-v4/ }));
    fireEvent.change(screen.getByRole('combobox'), {
      target: { value: 'deepseek-v4:chutes-api' },
    });
    fireEvent.change(screen.getByPlaceholderText(/Type a message/), {
      target: { value: 'Second session draft' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Model: DeepSeek V4' }));
    fireEvent.click(screen.getByRole('option', { name: /GLM 4.6, glm-4.6/ }));

    expect(screen.getByRole('combobox')).toHaveValue('');
    expect(screen.getByPlaceholderText(/Type a message/)).toHaveValue('Second session draft');
    expect(screen.getAllByRole('button', { name: /New session No messages/ })).toHaveLength(2);
    fireEvent.click(firstSession);
    expect(screen.getByRole('combobox')).toHaveValue('glm-4.6:chutes-api');
    expect(screen.getByPlaceholderText(/Type a message/)).toHaveValue('First session draft');
    expect(screen.getByPlaceholderText('You are a research assistant...')).toHaveValue(
      'First session instructions',
    );
  });

  it('disables model selection until the chat stream finishes', async () => {
    let finishStream: (response: ReturnType<typeof sseResponse>) => void = () => {};
    const pendingResponse = new Promise<ReturnType<typeof sseResponse>>((resolve) => {
      finishStream = resolve;
    });
    fetchWithAuth.mockImplementation(async (_base: string, path: string) =>
      path === '/internal/playground/models' ? MODELS : pendingResponse,
    );
    render(<PlaygroundPage />);
    const picker = await screen.findByRole('button', { name: 'Model: GLM 4.6' });
    await sendPrompt();

    expect(picker).toBeDisabled();
    fireEvent.click(picker);
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    await act(async () => finishStream(sseResponse(['data: [DONE]\n\n'])));
    await waitFor(() => expect(picker).not.toBeDisabled());
  });

  it('keeps streaming into its own session while another session is active', async () => {
    const first = controlledStream();
    mockChats(first.response);
    render(<PlaygroundPage />);
    await screen.findByRole('button', { name: 'Model: GLM 4.6' });
    await sendPrompt('first question');

    // Leaving the streaming session is allowed again, and the fresh session's
    // controls are live while the background turn runs.
    fireEvent.click(screen.getByRole('button', { name: '+ New' }));
    expect(screen.getByPlaceholderText(/Type a message/)).toHaveValue('');
    expect(screen.getByLabelText('Temperature')).not.toBeDisabled();

    await act(async () => first.push(contentFrame('background ')));
    await act(async () => first.push(contentFrame('reply')));
    fireEvent.click(screen.getByRole('button', { name: /first question/ }));
    await waitFor(() => expect(screen.getByText(/background reply/)).toBeInTheDocument());
  });

  it('can start a stream in another session while one is running', async () => {
    const first = controlledStream();
    const second = controlledStream();
    mockChats(first.response, second.response);
    render(<PlaygroundPage />);
    await screen.findByRole('button', { name: 'Model: GLM 4.6' });
    await sendPrompt('one');
    fireEvent.click(screen.getByRole('button', { name: '+ New' }));
    await sendPrompt('two');

    // Both turns are in flight; each writes into its own session.
    await act(async () => first.push(contentFrame('reply-one')));
    await act(async () => second.push(contentFrame('reply-two')));
    await act(async () => {
      first.end();
      second.end();
    });
    await waitFor(() => expect(screen.getByText(/reply-two/)).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /^one \d/ }));
    expect(screen.getByText(/reply-one/)).toBeInTheDocument();
    // Back to idle: the label reads Send again (disabled only because the
    // editor is empty, not because something is streaming).
    expect(screen.getByRole('button', { name: 'Send' })).toHaveTextContent('Send');
  });

  it('protects the streaming session from deletion but not the others', async () => {
    const first = controlledStream();
    mockChats(first.response);
    render(<PlaygroundPage />);
    await screen.findByRole('button', { name: 'Model: GLM 4.6' });
    await sendPrompt('busy');
    fireEvent.click(screen.getByRole('button', { name: '+ New' }));

    expect(screen.queryByRole('button', { name: /Delete session busy/ })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /Delete session New session/ }));
    expect(screen.queryByRole('button', { name: /Delete session New session/ })).toBeNull();

    // The background stream survives the deletion of the other session.
    await act(async () => first.push(contentFrame('still streaming')));
    fireEvent.click(screen.getByRole('button', { name: /busy/ }));
    await waitFor(() => expect(screen.getByText(/still streaming/)).toBeInTheDocument());
  });

  it('stops only the active session stream', async () => {
    const first = controlledStream();
    mockChats(first.response);
    render(<PlaygroundPage />);
    await screen.findByRole('button', { name: 'Model: GLM 4.6' });
    await sendPrompt('stoppable');
    fireEvent.click(screen.getByRole('button', { name: '+ New' }));

    // Another session's stream shows no Stop control here.
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /stoppable/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    // Aborting makes the parked read reject, exactly as a real reader does.
    await act(async () => first.failRead());
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull());
  });

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
    await screen.findByText('Error: Lost the connection to the server mid-reply.');
    expect(screen.getByTitle(/endpoint: glm-4\.6:zai-api/)).toBeInTheDocument();
  });

  it('leaves a failed turn out of the next request', async () => {
    mockChats(
      errorResponse('upstream down'),
      sseResponse([contentFrame('ok'), 'data: [DONE]\n\n']),
    );
    render(<PlaygroundPage />);
    await sendPrompt('first');
    expect(await screen.findByText('Error: upstream down')).toBeInTheDocument();

    await sendPrompt('second');
    await screen.findByText('ok');
    expect(sentHistories()[1]).toEqual([{ role: 'user', content: 'second' }]);
  });

  it('keeps the partial reply when the stream dies mid-answer', async () => {
    const stream = controlledStream();
    mockChats(stream.response);
    render(<PlaygroundPage />);
    await sendPrompt();
    stream.push(contentFrame('partial answer'));
    await screen.findByText('partial answer');
    stream.push('data: {"error": {"message": "connection reset"}}\n\n');
    stream.end();

    expect(await screen.findByText('Error: connection reset')).toBeInTheDocument();
    expect(screen.getByText('partial answer')).toBeInTheDocument();
  });

  it('flags an empty response as a failure', async () => {
    mockStream(['data: [DONE]\n\n']);
    render(<PlaygroundPage />);
    await sendPrompt();
    expect(
      await screen.findByText('Error: The model returned an empty response.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('retries a failed turn with the same question', async () => {
    mockChats(
      errorResponse('upstream down'),
      sseResponse([contentFrame('second try'), 'data: [DONE]\n\n']),
    );
    render(<PlaygroundPage />);
    await sendPrompt();
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));

    await screen.findByText('second try');
    expect(screen.queryByText(/upstream down/)).toBeNull();
    expect(sentHistories()[1]).toEqual([{ role: 'user', content: 'hi' }]);
  });

  it('surfaces a model-list failure with a retry', async () => {
    fetchWithAuth.mockRejectedValueOnce(new Error('Service unavailable')).mockResolvedValue(MODELS);
    render(<PlaygroundPage />);

    expect(
      await screen.findByText(/Couldn't load models: Service unavailable/),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Model: Models unavailable' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('button', { name: 'Model: GLM 4.6' })).toBeEnabled();
    expect(screen.queryByText(/Couldn't load models/)).toBeNull();
  });

  it('says when the reply was cut off by Max Tokens', async () => {
    mockStream([
      contentFrame('partial'),
      frame({ choices: [{ index: 0, delta: {}, finish_reason: 'length' }] }),
      'data: [DONE]\n\n',
    ]);
    render(<PlaygroundPage />);
    await sendPrompt();
    expect(await screen.findByText(/Cut off at the Max Tokens limit/)).toBeInTheDocument();
  });

  it('marks a stopped reply and offers a retry', async () => {
    fetchWithAuth.mockImplementation(
      async (_base: string, path: string, init?: { signal?: AbortSignal }) =>
        path === '/internal/playground/models'
          ? MODELS
          : new Promise((_resolve, reject) =>
              init?.signal?.addEventListener('abort', () =>
                reject(new DOMException('Aborted', 'AbortError')),
              ),
            ),
    );
    render(<PlaygroundPage />);
    await sendPrompt();
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));

    expect(await screen.findByText('Stopped before any output.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('keeps the editor usable while a reply streams', async () => {
    const stream = controlledStream();
    mockChats(stream.response);
    render(<PlaygroundPage />);
    await sendPrompt();

    const editor = screen.getByRole('textbox', { name: 'Message' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sending...' })).toBeDisabled());
    expect(editor).toBeEnabled();
    fireEvent.change(editor, { target: { value: 'next question' } });
    expect(editor).toHaveValue('next question');
    stream.push(contentFrame('done'));
    stream.end();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled());
    expect(editor).toHaveValue('next question');
  });

  it('moves the last question back into the editor, with undo', async () => {
    mockStream([contentFrame('answer'), 'data: [DONE]\n\n']);
    render(<PlaygroundPage />);
    await sendPrompt('my question');
    await screen.findByText('answer');

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('my question');
    expect(screen.queryByText('answer')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    expect(screen.getByText('answer')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('');
  });

  it('can undo clearing the conversation', async () => {
    mockStream([contentFrame('answer'), 'data: [DONE]\n\n']);
    render(<PlaygroundPage />);
    await sendPrompt();
    await screen.findByText('answer');

    fireEvent.click(screen.getByRole('button', { name: 'Clear conversation' }));
    expect(screen.getByText('Start a chat run')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    expect(screen.getByText('answer')).toBeInTheDocument();
  });

  it('can undo deleting a session', async () => {
    fetchWithAuth.mockResolvedValue(MODELS);
    render(<PlaygroundPage />);
    await screen.findByRole('button', { name: 'Model: GLM 4.6' });
    fireEvent.click(screen.getByRole('button', { name: '+ New' }));
    expect(screen.getAllByRole('button', { name: /^Delete session/ })).toHaveLength(2);

    fireEvent.click(screen.getAllByRole('button', { name: /^Delete session/ })[0]);
    expect(screen.queryAllByRole('button', { name: /^Delete session/ })).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    expect(screen.getAllByRole('button', { name: /^Delete session/ })).toHaveLength(2);
  });

  it('starts a new session with the current settings', async () => {
    fetchWithAuth.mockResolvedValue(MODELS);
    render(<PlaygroundPage />);
    await screen.findByRole('button', { name: 'Model: GLM 4.6' });
    fireEvent.change(screen.getByLabelText('Temperature'), { target: { value: '1.5' } });
    fireEvent.change(screen.getByLabelText('Max Tokens'), { target: { value: '8192' } });
    fireEvent.change(screen.getByLabelText('System instructions'), {
      target: { value: 'Be terse.' },
    });

    fireEvent.click(screen.getByRole('button', { name: '+ New' }));
    expect(screen.getByLabelText('Temperature')).toHaveValue('1.5');
    expect(screen.getByLabelText('Max Tokens')).toHaveValue('8192');
    expect(screen.getByLabelText('System instructions')).toHaveValue('Be terse.');
  });

  it('keeps reasoning expansion to the message it was opened on', async () => {
    mockStream([
      frame({ choices: [{ index: 0, delta: { reasoning_content: 'pondering' } }] }),
      contentFrame('answer'),
      'data: [DONE]\n\n',
    ]);
    render(<PlaygroundPage />);
    await sendPrompt();
    fireEvent.click(await screen.findByRole('button', { name: /^(Thought for|Thinking)/ }));
    expect(screen.getByText('pondering')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '+ New' }));
    await sendPrompt();
    await screen.findByRole('button', { name: /^(Thought for|Thinking)/ });
    expect(screen.queryByText('pondering')).toBeNull();
  });

  it('reports a failed copy instead of claiming success', async () => {
    Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true });
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
      configurable: true,
    });
    document.execCommand = vi.fn(() => false);
    mockStream([contentFrame('answer'), 'data: [DONE]\n\n']);
    render(<PlaygroundPage />);
    await sendPrompt();
    await screen.findByText('answer');

    fireEvent.click(screen.getAllByRole('button', { name: 'Copy message' })[1]);
    expect(await screen.findByRole('button', { name: 'Copy failed' })).toBeInTheDocument();
  });

  it('stops following the stream once the reader scrolls up', async () => {
    const stream = controlledStream();
    mockChats(stream.response);
    const { container } = render(<PlaygroundPage />);
    await sendPrompt();
    const chat = container.querySelector<HTMLElement>(
      '.dark-scrollbar.flex-1.overflow-y-auto.px-6',
    )!;
    const scrollTo = vi.mocked(Element.prototype.scrollTo);
    const place = (top: number) => {
      Object.defineProperty(chat, 'scrollHeight', { value: 1000, configurable: true });
      Object.defineProperty(chat, 'clientHeight', { value: 400, configurable: true });
      Object.defineProperty(chat, 'scrollTop', { value: top, configurable: true });
      fireEvent.scroll(chat);
    };
    place(600);
    place(200);
    expect(screen.getByRole('button', { name: /Jump to latest/ })).toBeInTheDocument();

    scrollTo.mockClear();
    stream.push(contentFrame('more text'));
    await screen.findByText('more text');
    expect(scrollTo).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /Jump to latest/ }));
    expect(scrollTo).toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /Jump to latest/ })).toBeNull();
    stream.end();
  });

  it('does not scroll the visible session while a background session streams', async () => {
    const first = controlledStream();
    mockChats(first.response);
    render(<PlaygroundPage />);
    await screen.findByRole('button', { name: 'Model: GLM 4.6' });
    await sendPrompt('background turn');

    // Switch to a fresh session C; the first turn keeps streaming in secret.
    fireEvent.click(screen.getByRole('button', { name: '+ New' }));
    const scrollTo = vi.mocked(Element.prototype.scrollTo);
    scrollTo.mockClear();

    // Background deltas flush into their own session but must not yank the
    // visible session's scroll — even though the stream began while its
    // session was the visible one.
    await act(async () => first.push(contentFrame('background ')));
    await act(async () => first.push(contentFrame('deltas')));
    await act(async () => first.push(routeFrame()));
    await act(async () => first.end());

    expect(scrollTo).not.toHaveBeenCalled();

    // The background turn did land in its own session.
    fireEvent.click(screen.getByRole('button', { name: /^background turn/ }));
    await waitFor(() => expect(screen.getByText(/background deltas/)).toBeInTheDocument());
  });

  it('follows the stream of the session the reader is watching', async () => {
    const first = controlledStream();
    mockChats(first.response);
    render(<PlaygroundPage />);
    await screen.findByRole('button', { name: 'Model: GLM 4.6' });
    await sendPrompt('watched turn');

    const scrollTo = vi.mocked(Element.prototype.scrollTo);
    scrollTo.mockClear();
    await act(async () => first.push(contentFrame('visible ')));
    await act(async () => first.push(contentFrame('deltas')));
    await act(async () => first.end());

    // The visible session's own stream still auto-scrolls.
    expect(scrollTo).toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText(/visible deltas/)).toBeInTheDocument());
  });

  it('never lets an old Undo overwrite a newer conversation', async () => {
    mockChats(
      sseResponse([contentFrame('first answer'), 'data: [DONE]\n\n']),
      sseResponse([contentFrame('second answer'), 'data: [DONE]\n\n']),
    );
    render(<PlaygroundPage />);
    await sendPrompt('first');
    await screen.findByText('first answer');
    fireEvent.click(screen.getByRole('button', { name: 'Clear conversation' }));
    expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument();

    await sendPrompt('second');
    await screen.findByText('second answer');
    expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull();
    expect(screen.queryByText('first answer')).toBeNull();
  });

  it('expires the Undo of an idle session while another session streams', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const first = controlledStream();
      mockChats(first.response);
      render(<PlaygroundPage />);
      await screen.findByRole('button', { name: 'Model: GLM 4.6' });
      await sendPrompt('streaming one');

      // Session C: idle, gets the Undo action. Its Undo is live even though
      // session A streams — the pause is per session, not global.
      fireEvent.click(screen.getByRole('button', { name: '+ New' }));
      fireEvent.click(screen.getAllByRole('button', { name: /Delete session/ })[0]);
      expect(screen.getByRole('button', { name: 'Undo' })).toBeEnabled();

      // The countdown runs to expiry while A's stream keeps going.
      await act(() => vi.advanceTimersByTimeAsync(8_000));
      expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull();

      // And A's stream is unaffected: it still lands its deltas.
      await act(async () => first.push(contentFrame(' done')));
      fireEvent.click(screen.getByRole('button', { name: /^streaming one/ }));
      await act(async () => first.end());
      await waitFor(() => expect(screen.getByText(/done/)).toBeInTheDocument());
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps the Undo timer paused until its own session stops streaming', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const first = controlledStream();
      mockChats(first.response);
      render(<PlaygroundPage />);
      await screen.findByRole('button', { name: 'Model: GLM 4.6' });
      await sendPrompt('busy session');
      await act(async () => first.push(contentFrame('partial')));
      await screen.findByText('partial');

      // Clearing mid-stream creates an Undo whose own session is still
      // streaming: paused, and seven seconds do not advance the countdown.
      fireEvent.click(screen.getByRole('button', { name: 'Clear conversation' }));
      expect(screen.getByRole('button', { name: 'Undo' })).toBeDisabled();
      await act(() => vi.advanceTimersByTimeAsync(7_000));
      expect(screen.getByRole('button', { name: 'Undo' })).toBeDisabled();

      // The turn winds down; the timer then runs its full span from the unpause.
      await act(async () => first.end());
      await waitFor(() => expect(screen.getByRole('button', { name: 'Undo' })).toBeEnabled());
      await act(() => vi.advanceTimersByTimeAsync(7_000));
      expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument();
      await act(() => vi.advanceTimersByTimeAsync(1_000));
      expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('aborts every live stream when the page unmounts', async () => {
    const first = controlledStream();
    const second = controlledStream();
    mockChats(first.response, second.response);
    const { unmount } = render(<PlaygroundPage />);
    await screen.findByRole('button', { name: 'Model: GLM 4.6' });
    await sendPrompt('first stream');
    fireEvent.click(screen.getByRole('button', { name: '+ New' }));
    await sendPrompt('second stream');

    // Both turns are parked mid-stream, one per session.
    const signals: AbortSignal[] = [];
    fetchWithAuth.mock.calls.forEach(([, , init]) => {
      const signal = (init as { signal?: AbortSignal } | undefined)?.signal;
      if (signal) signals.push(signal);
    });
    expect(signals).toHaveLength(2);
    expect(signals.every((s) => !s.aborted)).toBe(true);

    unmount();

    // Unmount pulled the plug on both streams; each turn's parked read
    // observes the abort through its own controller.
    expect(signals.every((s) => s.aborted)).toBe(true);
  });

  it('can undo a clear made mid-stream', async () => {
    const stream = controlledStream();
    mockChats(stream.response);
    render(<PlaygroundPage />);
    await sendPrompt();
    stream.push(contentFrame('partial'));
    await screen.findByText('partial');

    fireEvent.click(screen.getByRole('button', { name: 'Clear conversation' }));
    stream.end();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Undo' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    expect(screen.getByText('partial')).toBeInTheDocument();
    expect(screen.getByText('Stopped. The partial reply is kept as context.')).toBeInTheDocument();
  });

  it('ends the turn on an in-band error even if the connection stays open', async () => {
    const stream = controlledStream();
    mockChats(stream.response, sseResponse([contentFrame('fine'), 'data: [DONE]\n\n']));
    render(<PlaygroundPage />);
    await sendPrompt('first');
    stream.push(contentFrame('half an answer'));
    await screen.findByText('half an answer');
    stream.push(frame({ error: { message: 'upstream reset' } }));

    // No end(): the upstream never closes, yet the turn is over and still an error.
    expect(await screen.findByText('Error: upstream reset')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull());
    await sendPrompt('second');
    await screen.findByText('fine');
    expect(sentHistories()[1]).toEqual([{ role: 'user', content: 'second' }]);
  });

  it('says the connection was lost when the stream breaks mid-reply', async () => {
    const encoder = new TextEncoder();
    let reads = 0;
    const broken = {
      ok: true,
      body: {
        getReader: () => ({
          read: async () => {
            reads += 1;
            if (reads === 1) return { done: false, value: encoder.encode(contentFrame('half')) };
            // What Firefox rejects with when the response is cut off.
            throw new TypeError('Error in input stream');
          },
        }),
      },
    };
    mockChats(broken);
    render(<PlaygroundPage />);
    await sendPrompt();

    expect(
      await screen.findByText('Error: Lost the connection to the server mid-reply.'),
    ).toBeInTheDocument();
    expect(screen.getByText('half')).toBeInTheDocument();
    expect(screen.queryByText(/Error in input stream/)).toBeNull();
  });

  describe('a connection that goes quiet', () => {
    beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
    afterEach(() => vi.useRealTimers());

    it('gives up after 45 s of silence and keeps it out of context', async () => {
      // Sends one frame, then nothing, until the request is aborted -- as the
      // console's proxy does once the backend has dropped.
      const encoder = new TextEncoder();
      let sent = false;
      fetchWithAuth.mockImplementation(
        async (_base: string, path: string, init?: { signal?: AbortSignal }) => {
          if (path === '/internal/playground/models') return MODELS;
          if (sent) return sseResponse([contentFrame('fine'), 'data: [DONE]\n\n']);
          sent = true;
          let first = true;
          const read = () =>
            first
              ? ((first = false),
                Promise.resolve({ done: false, value: encoder.encode(contentFrame('half')) }))
              : new Promise((_resolve, reject) =>
                  init?.signal?.addEventListener('abort', () =>
                    reject(new DOMException('aborted', 'AbortError')),
                  ),
                );
          return { ok: true, body: { getReader: () => ({ read }) } };
        },
      );
      render(<PlaygroundPage />);
      await sendPrompt('first');
      await screen.findByText('half');

      await act(() => vi.advanceTimersByTimeAsync(44_000));
      expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument();
      await act(() => vi.advanceTimersByTimeAsync(1_000));

      expect(
        await screen.findByText(/^Error: Lost the connection: nothing from the server for 45 s\./),
      ).toBeInTheDocument();
      expect(screen.getByText('half')).toBeInTheDocument();
      expect(screen.queryByText(/^Stopped/)).toBeNull();
      await sendPrompt('second');
      await screen.findByText('fine');
      expect(sentHistories()[1]).toEqual([{ role: 'user', content: 'second' }]);
    });

    it('keeps waiting while keepalives arrive', async () => {
      const stream = controlledStream();
      mockChats(stream.response);
      render(<PlaygroundPage />);
      await sendPrompt();
      for (let i = 0; i < 8; i += 1) {
        await act(() => vi.advanceTimersByTimeAsync(15_000));
        stream.push(': keepalive\n\n');
      }
      stream.push(contentFrame('worth the wait'));
      stream.push('data: [DONE]\n\n');
      stream.end();

      expect(await screen.findByText('worth the wait')).toBeInTheDocument();
      expect(screen.queryByText(/^Error:/)).toBeNull();
    });
  });
});
