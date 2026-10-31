// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  BatchConsoleError,
  createConsoleBatch,
  findSubmission,
  getCreateContext,
  getMyBatch,
  preflightSource,
  uploadSourceFile,
  type BatchCreateContext,
  type BatchDetail,
  type PreflightReport,
} from '@/lib/api/batches';

import { CreateBatchView } from './CreateBatchView';

let currentUser: string | null = 'alice';
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: currentUser ? { id: currentUser } : null } }),
}));
vi.mock('@/lib/hooks/queries/useBatchCapabilities', () => ({
  useBatchCapabilities: () => ({
    capabilities: { role_may_submit: true, min_role: 'internal' },
    loading: false,
    error: null,
  }),
}));
const push = vi.fn();
vi.mock('next/navigation', () => ({ useRouter: () => ({ push }) }));
vi.mock('next/link', () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));
vi.mock('@/lib/hooks/useAccountToast', () => ({
  useAccountToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));
vi.mock('@/lib/api/batches', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/batches')>();
  return {
    ...actual,
    getCreateContext: vi.fn(),
    getMyBatch: vi.fn(),
    uploadSourceFile: vi.fn(),
    preflightSource: vi.fn(),
    createConsoleBatch: vi.fn(),
    findSubmission: vi.fn(),
    listSourceFiles: vi.fn(async () => ({
      data: [],
      has_more: false,
      usage: { stored_bytes: 0, uploads_last_hour: 0 },
      files_max_total_bytes: 1,
    })),
  };
});

function context(overrides: Partial<BatchCreateContext> = {}): BatchCreateContext {
  return {
    user_id: currentUser ?? 'alice',
    role_may_submit: true,
    min_role: 'internal',
    submission_enabled: true,
    limits: {
      max_file_bytes: 32 * 1024 * 1024,
      max_line_bytes: 4 * 1024 * 1024,
      max_requests: 50000,
      uploads_per_hour: 60,
      files_max_total_bytes: 512 * 1024 * 1024,
      max_active_batches: 10,
      file_retention_days: 30,
      result_retention_days: 7,
      completion_window_hours: 24,
      submission_replay_seconds: 86400,
    },
    usage: { stored_bytes: 0, uploads_last_hour: 0, active_batches: 0 },
    credential: { key_id: '7', key_prefix: 'hyi-xxxxxxxx', revision: 'rev-1' },
    quota: null,
    models: {
      chat: [
        {
          id: 'model-z',
          name: 'Model Z',
          max_output_length: 4096,
          supported_sampling_parameters: [],
        },
      ],
      embeddings: [
        { id: 'embed-1', name: 'Embed', max_output_length: 0, supported_sampling_parameters: [] },
      ],
    },
    ...overrides,
  };
}

function report(
  overrides: Partial<PreflightReport> = {},
  type: 'chat' | 'embeddings' | null = 'chat',
): PreflightReport {
  return {
    source: {
      id: 'file-1',
      filename: 'requests.jsonl',
      sha256: 'f'.repeat(64),
      bytes: 120,
      lines: 2,
    },
    detected: {
      type,
      endpoint: type === 'chat' ? '/v1/chat/completions' : type ? '/v1/embeddings' : null,
      source: type ? 'url' : null,
      needs_type_choice: false,
      rows: {
        chat: type === 'chat' ? 2 : 0,
        embeddings: type === 'embeddings' ? 2 : 0,
        undetermined: 0,
      },
      url_missing_rows: 0,
      models: [{ name: 'model-z', rows: 2, available: true, canonical: 'model-z' }],
      models_other_rows: 0,
      model_unset_rows: 0,
    },
    params: { temperature: { values: { '0': 2 }, other: 0, unset: 0 } },
    other_params: {},
    model_resolution: null,
    valid: false,
    request_count: 0,
    stores_copy: false,
    error_count: 0,
    errors_truncated: false,
    errors: [],
    preview: [],
    ...overrides,
  };
}

const VALID = report({
  valid: true,
  request_count: 2,
  model_resolution: { requested: 'model-z', canonical: 'model-z' },
  preview: [
    {
      line: 1,
      custom_id: 'q-1',
      filled: [],
      truncated: false,
      request: { custom_id: 'q-1', method: 'POST', url: '/v1/chat/completions', body: {} },
    },
  ],
});

function created(id = 'batch_new', userId = 'alice'): BatchDetail {
  return { id, user_id: userId } as BatchDetail;
}

function intent(userId: string, ageMs: number, key = 'console-old') {
  return {
    version: 1,
    userId,
    key,
    createdAt: Date.now() - ageMs,
    body: {
      source_file_id: 'file-1',
      source_sha256: 'f'.repeat(64),
      endpoint: '/v1/chat/completions',
      overrides: {},
      confirmed_model: { requested: 'model-z', canonical: 'model-z' },
      credential: { key_id: '7', revision: 'rev-1' },
      name: 'earlier run',
      description: '',
      copied_from: null,
    },
  };
}

async function uploadAFile() {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(['{}\n'], 'requests.jsonl', { type: 'application/jsonl' });
  fireEvent.change(input, { target: { files: [file] } });
}

beforeEach(() => {
  vi.clearAllMocks();
  currentUser = 'alice';
  window.localStorage.clear();
  Object.defineProperty(navigator, 'locks', {
    value: { request: async (_name: string, fn: () => Promise<unknown>) => fn() },
    configurable: true,
  });
  vi.mocked(getCreateContext).mockImplementation(async () => context());
  vi.mocked(findSubmission).mockResolvedValue({ state: 'unknown', batch: null });
  vi.mocked(uploadSourceFile).mockResolvedValue({
    id: 'file-1',
    bytes: 120,
    created_at: 1,
    expires_at: 2_000_000_000,
    filename: 'requests.jsonl',
    sha256: 'f'.repeat(64),
    line_count: 2,
  });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});
afterEach(cleanup);

describe('CreateBatchView', () => {
  it('recognizes the type, shows no type choice, and submits one intent', async () => {
    vi.mocked(preflightSource).mockImplementation(async (request) =>
      request.phase === 'identify' ? report() : VALID,
    );
    vi.mocked(createConsoleBatch).mockResolvedValue(created());
    render(<CreateBatchView copyFrom={null} />);
    await screen.findByText('Upload data', { selector: 'h2' });
    await uploadAFile();
    expect(await screen.findByTestId('recognized-type')).toHaveTextContent(
      'Recognized: Text generation (Chat completions) · 2 requests',
    );
    expect(screen.queryByText('Which type are these requests?')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(await screen.findByTestId('configure-type')).toHaveTextContent('Text generation');
    fireEvent.click(screen.getByRole('button', { name: 'Check and preview' }));
    expect(await screen.findByTestId('confirmation')).toHaveTextContent('API key hyi-xxxxxxxx…');
    expect(vi.mocked(preflightSource).mock.calls[1][0]).toMatchObject({
      phase: 'validate',
      endpoint: null,
    });
    fireEvent.click(screen.getByRole('button', { name: 'Confirm and submit' }));
    await waitFor(() => expect(push).toHaveBeenCalledWith('/dashboard/batches/batch_new'));
    const [body, key] = vi.mocked(createConsoleBatch).mock.calls[0];
    expect(key).toMatch(/^console-/);
    expect(body).toMatchObject({
      source_file_id: 'file-1',
      endpoint: '/v1/chat/completions',
      credential: { key_id: '7', revision: 'rev-1' },
      confirmed_model: { requested: 'model-z', canonical: 'model-z' },
      overrides: {},
    });
    // Settled: nothing is left pending.
    expect(window.localStorage.getItem('batch-submit:alice')).toBeNull();
  });

  it('asks for the type only when the file does not state it', async () => {
    vi.mocked(preflightSource).mockResolvedValue(
      report(
        {
          detected: {
            ...report().detected,
            type: null,
            endpoint: null,
            source: null,
            needs_type_choice: true,
            rows: { chat: 0, embeddings: 0, undetermined: 2 },
          },
        },
        null,
      ),
    );
    render(<CreateBatchView copyFrom={null} />);
    await screen.findByText('Upload data', { selector: 'h2' });
    await uploadAFile();
    expect(await screen.findByText('Which type are these requests?')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
    fireEvent.click(screen.getByLabelText('Text embeddings (Embeddings)'));
    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled();
  });

  it('keeps a submission whose answer was lost, and recovers it with the same key after a reload', async () => {
    vi.mocked(preflightSource).mockImplementation(async (request) =>
      request.phase === 'identify' ? report() : VALID,
    );
    vi.mocked(createConsoleBatch).mockRejectedValue(new TypeError('network down'));
    const first = render(<CreateBatchView copyFrom={null} />);
    await screen.findByText('Upload data', { selector: 'h2' });
    await uploadAFile();
    fireEvent.click(await screen.findByRole('button', { name: 'Next' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Check and preview' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm and submit' }));
    expect(await screen.findByText(/Confirming the result of your submission/)).toBeInTheDocument();
    const stored = JSON.parse(window.localStorage.getItem('batch-submit:alice') ?? '{}');
    const sentKey = vi.mocked(createConsoleBatch).mock.calls[0][1];
    expect(stored.key).toBe(sentKey);
    first.unmount();

    // The page is opened again: the batch did get created.
    vi.mocked(findSubmission).mockResolvedValue({ state: 'created', batch: created('batch_1') });
    vi.mocked(createConsoleBatch).mockClear();
    render(<CreateBatchView copyFrom={null} />);
    await waitFor(() => expect(push).toHaveBeenCalledWith('/dashboard/batches/batch_1'));
    expect(findSubmission).toHaveBeenCalledWith(sentKey);
    expect(createConsoleBatch).not.toHaveBeenCalled();
    expect(window.localStorage.getItem('batch-submit:alice')).toBeNull();
  });

  it('resends a recent unconfirmed submission with its own key', async () => {
    window.localStorage.setItem('batch-submit:alice', JSON.stringify(intent('alice', 60_000)));
    vi.mocked(createConsoleBatch).mockResolvedValue(created('batch_2'));
    render(<CreateBatchView copyFrom={null} />);
    await waitFor(() => expect(push).toHaveBeenCalledWith('/dashboard/batches/batch_2'));
    expect(vi.mocked(createConsoleBatch).mock.calls[0][1]).toBe('console-old');
  });

  it('never resends a stale submission by itself, and only replaces it on request', async () => {
    // Committed, response lost, batch finished, its record expired and was swept:
    // two days later nothing can be found under the key.
    window.localStorage.setItem(
      'batch-submit:alice',
      JSON.stringify(intent('alice', 2 * 86_400_000)),
    );
    render(<CreateBatchView copyFrom={null} />);
    expect(await screen.findByText(/too old to send again safely/)).toBeInTheDocument();
    expect(createConsoleBatch).not.toHaveBeenCalled();
    expect(window.localStorage.getItem('batch-submit:alice')).not.toBeNull();

    vi.mocked(createConsoleBatch).mockResolvedValue(created('batch_3'));
    fireEvent.click(screen.getByRole('button', { name: 'Submit again as a new batch' }));
    await waitFor(() => expect(push).toHaveBeenCalledWith('/dashboard/batches/batch_3'));
    expect(window.confirm).toHaveBeenCalled();
    const key = vi.mocked(createConsoleBatch).mock.calls[0][1];
    expect(key).not.toBe('console-old');
  });

  it('forgets a stale submission without sending anything', async () => {
    window.localStorage.setItem(
      'batch-submit:alice',
      JSON.stringify(intent('alice', 2 * 86_400_000)),
    );
    render(<CreateBatchView copyFrom={null} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Forget it' }));
    await waitFor(() => expect(window.localStorage.getItem('batch-submit:alice')).toBeNull());
    expect(createConsoleBatch).not.toHaveBeenCalled();
  });

  it("never shows or replays another account's pending submission", async () => {
    window.localStorage.setItem('batch-submit:bob', JSON.stringify(intent('bob', 60_000)));
    render(<CreateBatchView copyFrom={null} />);
    await screen.findByText('Upload data', { selector: 'h2' });
    expect(findSubmission).not.toHaveBeenCalled();
    expect(createConsoleBatch).not.toHaveBeenCalled();
    expect(screen.queryByText(/Confirming|too old/)).not.toBeInTheDocument();
    expect(window.localStorage.getItem('batch-submit:bob')).not.toBeNull();
  });

  it('reports a deleted batch and does not create another', async () => {
    window.localStorage.setItem('batch-submit:alice', JSON.stringify(intent('alice', 60_000)));
    vi.mocked(findSubmission).mockResolvedValue({ state: 'deleted', batch: null });
    render(<CreateBatchView copyFrom={null} />);
    expect(await screen.findByText(/has since been deleted/)).toBeInTheDocument();
    expect(createConsoleBatch).not.toHaveBeenCalled();
  });

  it('without an API key checks the file but sends the user to create a key', async () => {
    vi.mocked(getCreateContext).mockImplementation(async () => context({ credential: null }));
    vi.mocked(preflightSource).mockImplementation(async (request) =>
      request.phase === 'identify' ? report() : VALID,
    );
    render(<CreateBatchView copyFrom={null} />);
    await screen.findByText('Upload data', { selector: 'h2' });
    await uploadAFile();
    fireEvent.click(await screen.findByRole('button', { name: 'Next' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Check and preview' }));
    expect(await screen.findByRole('link', { name: 'Create an API key' })).toHaveAttribute(
      'href',
      '/dashboard',
    );
    expect(screen.getByRole('button', { name: 'Confirm and submit' })).toBeDisabled();
  });

  it('goes back to the check when the confirmed credential changed', async () => {
    vi.mocked(preflightSource).mockImplementation(async (request) =>
      request.phase === 'identify' ? report() : VALID,
    );
    vi.mocked(createConsoleBatch).mockRejectedValue(
      new BatchConsoleError(409, 'credential_changed', 'changed'),
    );
    render(<CreateBatchView copyFrom={null} />);
    await screen.findByText('Upload data', { selector: 'h2' });
    await uploadAFile();
    fireEvent.click(await screen.findByRole('button', { name: 'Next' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Check and preview' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm and submit' }));
    expect(await screen.findByText(/Your API key changed after you confirmed/)).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'Check and preview' })).toBeInTheDocument();
    expect(window.localStorage.getItem('batch-submit:alice')).toBeNull();
  });

  it('copies a batch onto its source and settings without running anything', async () => {
    vi.mocked(getMyBatch).mockResolvedValue({
      id: 'batch_old',
      user_id: 'alice',
      name: 'eval-0420',
      description: 'weekly',
      copy: { state: 'ready', reason: null },
      source: {
        id: 'file-1',
        state: 'available',
        filename: 'requests.jsonl',
        sha256: 'f'.repeat(64),
      },
      creation: { overrides: { model: 'model-z', temperature: 0 } },
    } as unknown as BatchDetail);
    vi.mocked(preflightSource).mockResolvedValue(report());
    render(<CreateBatchView copyFrom="batch_old" />);
    expect(await screen.findByTestId('configure-type')).toBeInTheDocument();
    expect(screen.getByDisplayValue('eval-0420-copy')).toBeInTheDocument();
    expect(screen.getByDisplayValue('weekly')).toBeInTheDocument();
    expect(createConsoleBatch).not.toHaveBeenCalled();
  });

  it('explains why a direct-request batch cannot be copied', async () => {
    vi.mocked(getMyBatch).mockResolvedValue({
      id: 'batch_inline',
      user_id: 'alice',
      copy: { state: 'unavailable', reason: 'inline_input' },
    } as unknown as BatchDetail);
    render(<CreateBatchView copyFrom="batch_inline" />);
    expect(await screen.findByText(/no input file to reuse/)).toBeInTheDocument();
    expect(screen.queryByText('Upload data', { selector: 'h2' })).not.toBeInTheDocument();
  });
});

// -- review round 2: obsolete answers, stale actions across tabs, account switches ------

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function sourceFile(id: string, filename: string) {
  return {
    id,
    bytes: 120,
    created_at: 1,
    expires_at: 2_000_000_000,
    filename,
    sha256: id.padEnd(64, '0'),
    line_count: 2,
  };
}

function reportFor(id: string, filename: string, overrides: Partial<PreflightReport> = {}) {
  return report({
    ...overrides,
    source: { id, filename, sha256: id.padEnd(64, '0'), bytes: 120, lines: 2 },
  });
}

function validFor(id: string, filename: string) {
  return reportFor(id, filename, {
    valid: true,
    request_count: 2,
    model_resolution: { requested: 'model-z', canonical: 'model-z' },
  });
}

function uploadNamed(name: string) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, {
    target: { files: [new File(['{}\n'], name, { type: 'application/jsonl' })] },
  });
}

describe('CreateBatchView: answers for what the user has left', () => {
  it('never shows or submits a check of a file the user has since replaced', async () => {
    const checkA = deferred<PreflightReport>();
    vi.mocked(uploadSourceFile).mockImplementation(async (file) =>
      file.name === 'a.jsonl' ? sourceFile('file-A', 'a.jsonl') : sourceFile('file-B', 'b.jsonl'),
    );
    vi.mocked(preflightSource).mockImplementation(async (request) => {
      const name = request.source_file_id === 'file-A' ? 'a.jsonl' : 'b.jsonl';
      if (request.phase === 'identify') return reportFor(request.source_file_id, name);
      if (request.source_file_id === 'file-A') return checkA.promise;
      return validFor('file-B', 'b.jsonl');
    });
    vi.mocked(createConsoleBatch).mockResolvedValue(created());
    render(<CreateBatchView copyFrom={null} />);
    await screen.findByText('Upload data', { selector: 'h2' });
    uploadNamed('a.jsonl');
    fireEvent.click(await screen.findByRole('button', { name: 'Next' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Check and preview' }));
    // A is still being checked: go back and use B instead.
    fireEvent.click(await screen.findByRole('button', { name: 'Back' }));
    uploadNamed('b.jsonl');
    expect(await screen.findByText(/b\.jsonl/)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: 'Next' }));
    await screen.findByRole('button', { name: 'Check and preview' });
    // A's answer arrives now; it changes nothing.
    checkA.resolve(validFor('file-A', 'a.jsonl'));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByTestId('confirmation')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Check and preview' })).toBeInTheDocument();
    // B, checked and submitted, is what is sent.
    fireEvent.click(screen.getByRole('button', { name: 'Check and preview' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm and submit' }));
    await waitFor(() => expect(createConsoleBatch).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createConsoleBatch).mock.calls[0][0].source_file_id).toBe('file-B');
  });

  it('keeps the newer of two uploads and abandons the older', async () => {
    const slowA = deferred<ReturnType<typeof sourceFile>>();
    let signalA: AbortSignal | undefined;
    vi.mocked(uploadSourceFile).mockImplementation(async (file, options) => {
      if (file.name === 'a.jsonl') {
        signalA = options?.signal;
        return slowA.promise;
      }
      return sourceFile('file-B', 'b.jsonl');
    });
    vi.mocked(preflightSource).mockImplementation(async (request) =>
      reportFor(
        request.source_file_id,
        request.source_file_id === 'file-A' ? 'a.jsonl' : 'b.jsonl',
      ),
    );
    render(<CreateBatchView copyFrom={null} />);
    await screen.findByText('Upload data', { selector: 'h2' });
    uploadNamed('a.jsonl');
    uploadNamed('b.jsonl');
    expect(await screen.findByText(/b\.jsonl/)).toBeInTheDocument();
    expect(signalA?.aborted).toBe(true);
    slowA.resolve(sourceFile('file-A', 'a.jsonl'));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText(/a\.jsonl/)).not.toBeInTheDocument();
    expect(
      vi.mocked(preflightSource).mock.calls.map(([request]) => request.source_file_id),
    ).toEqual(['file-B']);
  });

  it('drops a temperature the newly chosen model does not take, visibly', async () => {
    vi.mocked(getCreateContext).mockImplementation(async () =>
      context({
        models: {
          chat: [
            {
              id: 'model-z',
              name: 'Z',
              max_output_length: 0,
              supported_sampling_parameters: ['temperature', 'max_tokens'],
            },
            { id: 'bare', name: 'Bare', max_output_length: 0, supported_sampling_parameters: [] },
          ],
          embeddings: [],
        },
      }),
    );
    window.localStorage.setItem(
      'batch-draft:alice',
      JSON.stringify({
        userId: 'alice',
        source: sourceFile('file-1', 'requests.jsonl'),
        typeChoice: null,
        name: 'run',
        nameEdited: true,
        description: '',
        modelMode: 'override',
        model: 'bare',
        temperatureMode: 'set',
        temperature: '0.7',
        maxTokensMode: 'set',
        maxTokens: '64',
        copiedFrom: null,
        originalSha256: null,
      }),
    );
    vi.mocked(preflightSource).mockImplementation(async (request) =>
      request.phase === 'identify' ? report() : VALID,
    );
    render(<CreateBatchView copyFrom={null} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Next' }));
    expect(await screen.findByTestId('params-not-taken')).toHaveTextContent(
      'does not take temperature, max_tokens',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Check and preview' }));
    await waitFor(() => expect(preflightSource).toHaveBeenCalledTimes(2));
    expect(vi.mocked(preflightSource).mock.calls[1][0].overrides).toEqual({ model: 'bare' });
  });
});

describe('CreateBatchView: a stale submission acted on in two tabs', () => {
  const stale = intent('alice', 2 * 86_400_000, 'console-K');
  const newer = intent('alice', 60_000, 'console-K2');

  async function viaLocalStorage() {
    window.localStorage.setItem('batch-submit:alice', JSON.stringify(stale));
    return {
      replace: async () => window.localStorage.setItem('batch-submit:alice', JSON.stringify(newer)),
    };
  }

  async function viaIndexedDb() {
    const { fakeIndexedDb } = await import('@/lib/batches/__tests__/fakeIndexedDb');
    const { indexedDbIntentStore } = await import('@/lib/batches/submitIntent');
    Object.defineProperty(navigator, 'locks', { value: undefined, configurable: true });
    const factory = fakeIndexedDb();
    (globalThis as { indexedDB?: IDBFactory }).indexedDB = factory;
    const store = indexedDbIntentStore(factory);
    await store.createIfAbsent(stale as never);
    return {
      replace: async () => {
        await store.replaceIf('alice', 'console-K', newer as never);
      },
    };
  }

  afterEach(() => {
    delete (globalThis as { indexedDB?: IDBFactory }).indexedDB;
  });

  for (const [persistence, seed] of [
    ['Web Locks + localStorage', viaLocalStorage],
    ['IndexedDB', viaIndexedDb],
  ] as const) {
    for (const action of ['Submit again as a new batch', 'Forget it'] as const) {
      it(`"${action}" recovers what another tab made instead of replacing it (${persistence})`, async () => {
        const tab = await seed();
        vi.mocked(createConsoleBatch).mockResolvedValue(created('batch_k2'));
        render(<CreateBatchView copyFrom={null} />);
        expect(await screen.findByText(/too old to send again safely/)).toBeInTheDocument();
        // Tab 1 already chose "submit again" (K2), and its answer was lost.
        await tab.replace();
        fireEvent.click(screen.getByRole('button', { name: action }));
        await waitFor(() => expect(push).toHaveBeenCalledWith('/dashboard/batches/batch_k2'));
        const keys = vi.mocked(createConsoleBatch).mock.calls.map(([, key]) => key);
        expect(keys).toEqual(['console-K2']);
        expect(findSubmission).toHaveBeenCalledWith('console-K2');
      });
    }
  }
});

describe('CreateBatchView: an account switch while the intent lock is awaited', () => {
  it("never sends the previous account's submission", async () => {
    const gate = deferred<void>();
    Object.defineProperty(navigator, 'locks', {
      value: {
        request: async (_name: string, fn: () => Promise<unknown>) => gate.promise.then(fn),
      },
      configurable: true,
    });
    window.localStorage.setItem(
      'batch-submit:alice',
      JSON.stringify(intent('alice', 60_000, 'console-A')),
    );
    const view = render(<CreateBatchView copyFrom={null} />);
    currentUser = 'bob';
    vi.mocked(getCreateContext).mockImplementation(async () => context());
    view.rerender(<CreateBatchView copyFrom={null} />);
    gate.resolve();
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(findSubmission).not.toHaveBeenCalledWith('console-A');
    expect(createConsoleBatch).not.toHaveBeenCalled();
    expect(window.localStorage.getItem('batch-submit:alice')).not.toBeNull();
  });
});

describe('CreateBatchView: the upload step', () => {
  it('leads with choosing a file, which opens the file picker', async () => {
    const click = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => {});
    render(<CreateBatchView copyFrom={null} />);
    await screen.findByText('Upload data', { selector: 'h2' });
    fireEvent.click(screen.getByRole('button', { name: 'Choose file' }));
    expect(click).toHaveBeenCalledTimes(1);
    expect(click.mock.instances[0]).toBe(document.querySelector('input[type="file"]'));
    // One short statement and the example; no separate format section, no static samples.
    expect(document.querySelector('details')).toBeNull();
    expect(document.querySelector('a[href^="/samples/"]')).toBeNull();
    expect(screen.getByRole('button', { name: 'Copy example' })).toBeInTheDocument();
    click.mockRestore();
  });

  it('uploads a dropped file and then offers to choose another', async () => {
    vi.mocked(preflightSource).mockResolvedValue(report());
    render(<CreateBatchView copyFrom={null} />);
    await screen.findByText('Upload data', { selector: 'h2' });
    const file = new File(['{}\n'], 'requests.jsonl', { type: 'application/jsonl' });
    fireEvent.drop(screen.getByTestId('drop-zone'), { dataTransfer: { files: [file] } });
    await waitFor(() => expect(uploadSourceFile).toHaveBeenCalled());
    expect(vi.mocked(uploadSourceFile).mock.calls[0][0]).toBe(file);
    expect(await screen.findByTestId('recognized-type')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Choose another file' })).toBeInTheDocument();
  });

  it('still refuses a file that is not .jsonl before uploading it', async () => {
    render(<CreateBatchView copyFrom={null} />);
    await screen.findByText('Upload data', { selector: 'h2' });
    fireEvent.drop(screen.getByTestId('drop-zone'), {
      dataTransfer: { files: [new File(['a,b\n'], 'requests.csv', { type: 'text/csv' })] },
    });
    expect(await screen.findByRole('alert')).toHaveTextContent('Choose a .jsonl file.');
    expect(uploadSourceFile).not.toHaveBeenCalled();
  });

  it('asks for one model in step 2 when the file names none, as the format says', async () => {
    const noModel = report({
      detected: { ...report().detected, source: 'structure', models: [], model_unset_rows: 2 },
    });
    vi.mocked(preflightSource).mockImplementation(async (request) =>
      request.phase === 'identify' ? noModel : VALID,
    );
    render(<CreateBatchView copyFrom={null} />);
    await screen.findByText('Upload data', { selector: 'h2' });
    await uploadAFile();
    expect(await screen.findByText('No model: 2 lines')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByTestId('configure-type');
    expect(screen.getByLabelText(/Use the file's model \(not possible/)).toBeDisabled();
    expect(screen.getByLabelText('Use one model for every request:')).toBeChecked();
    expect(screen.getByRole('button', { name: 'Check and preview' })).toBeDisabled();
  });
});

describe('CreateBatchView: a new file whose identification fails', () => {
  it("never keeps the previous file's identification", async () => {
    let rejectB!: (error: unknown) => void;
    const failingB = new Promise<PreflightReport>((_resolve, reject) => {
      rejectB = reject;
    });
    vi.mocked(uploadSourceFile).mockImplementation(async (file) =>
      file.name === 'a.jsonl' ? sourceFile('file-A', 'a.jsonl') : sourceFile('file-B', 'b.jsonl'),
    );
    vi.mocked(preflightSource).mockImplementation(async (request) =>
      request.source_file_id === 'file-A' ? reportFor('file-A', 'a.jsonl') : failingB,
    );
    render(<CreateBatchView copyFrom={null} />);
    await screen.findByText('Upload data', { selector: 'h2' });
    uploadNamed('a.jsonl');
    expect(await screen.findByTestId('recognized-type')).toBeInTheDocument();
    uploadNamed('b.jsonl');
    // B is uploaded and being read: A's report is gone at once.
    await waitFor(() => expect(screen.queryByTestId('recognized-type')).not.toBeInTheDocument());
    rejectB(new TypeError('network down'));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByTestId('recognized-type')).not.toBeInTheDocument();
    expect(screen.queryByText(/a\.jsonl/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  });

  it('uploads for the account the page shows, named explicitly', async () => {
    vi.mocked(preflightSource).mockResolvedValue(report());
    render(<CreateBatchView copyFrom={null} />);
    await screen.findByText('Upload data', { selector: 'h2' });
    await uploadAFile();
    await waitFor(() => expect(uploadSourceFile).toHaveBeenCalled());
    expect(vi.mocked(uploadSourceFile).mock.calls[0][1]).toMatchObject({ expectedUserId: 'alice' });
  });
});
