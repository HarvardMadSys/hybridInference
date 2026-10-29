// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

type Client = typeof import('../client');

const API = 'https://console.example';

function token(sub: string, expiresInSeconds: number): string {
  const encode = (value: object) =>
    btoa(JSON.stringify(value)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
  const exp = Math.floor(Date.now() / 1000) + expiresInSeconds;
  return `${encode({ alg: 'HS256' })}.${encode({ sub, exp })}.signature`;
}

/** An XHR that answers each send with the next queued status, reporting progress first. */
class FakeXhr {
  static answers: number[] = [];
  static sent: { url: string; authorization: string | null }[] = [];
  /** Runs once, just before the next answer arrives (another tab acting meanwhile). */
  static beforeAnswer: (() => void) | null = null;
  upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null };
  status = 0;
  responseText = '';
  withCredentials = false;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  private url = '';
  private headers: Record<string, string> = {};
  open(_method: string, url: string) {
    this.url = url;
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }
  getResponseHeader() {
    return 'application/json';
  }
  abort() {
    this.onabort?.();
  }
  send() {
    FakeXhr.sent.push({ url: this.url, authorization: this.headers.Authorization ?? null });
    const status = FakeXhr.answers.shift() ?? 200;
    queueMicrotask(() => {
      const before = FakeXhr.beforeAnswer;
      FakeXhr.beforeAnswer = null;
      before?.();
      this.upload.onprogress?.({ loaded: 5, total: 10, lengthComputable: true } as ProgressEvent);
      this.status = status;
      this.responseText = JSON.stringify({ id: 'file-1' });
      this.onload?.();
    });
  }
}

let client: Client;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(async () => {
  vi.resetModules();
  client = await import('../client');
  sessionStorage.clear();
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('XMLHttpRequest', FakeXhr);
  FakeXhr.answers = [];
  FakeXhr.sent = [];
  FakeXhr.beforeAnswer = null;
});

afterEach(() => vi.unstubAllGlobals());

const AS_A = { expectedUserId: 'user-a' };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe('uploading with the session', () => {
  it('sends the token and reports progress', async () => {
    const current = token('user-a', 900);
    client.setAccessToken(current);
    const progress = vi.fn();
    const resp = await client.uploadWithAuth(API, '/user/batch-files', new FormData(), {
      ...AS_A,
      onProgress: progress,
    });
    expect(resp.status).toBe(200);
    expect(FakeXhr.sent).toEqual([
      { url: `${API}/user/batch-files`, authorization: `Bearer ${current}` },
    ]);
    expect(progress).toHaveBeenCalledWith({ loaded: 5, total: 10 });
  });

  it('refreshes after a 401 and sends the upload again as the same account', async () => {
    client.setAccessToken(token('user-a', 900));
    const renewed = token('user-a', 1800);
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ access_token: renewed }), { status: 200 }),
    );
    FakeXhr.answers = [401, 200];
    const resp = await client.uploadWithAuth(API, '/user/batch-files', new FormData(), AS_A);
    expect(resp.status).toBe(200);
    expect(FakeXhr.sent[1].authorization).toBe(`Bearer ${renewed}`);
  });

  it('never uploads into an account another tab switched to', async () => {
    client.setAccessToken(token('user-a', -60));
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ access_token: token('user-b', 900) }), { status: 200 }),
    );
    await expect(
      client.uploadWithAuth(API, '/user/batch-files', new FormData(), AS_A),
    ).rejects.toMatchObject({ code: 'ACCOUNT_CHANGED' });
    expect(FakeXhr.sent).toEqual([]);
  });

  it('is cancelled through its signal', async () => {
    client.setAccessToken(token('user-a', 900));
    const controller = new AbortController();
    controller.abort();
    await expect(
      client.uploadWithAuth(API, '/user/batch-files', new FormData(), {
        ...AS_A,
        signal: controller.signal,
      }),
    ).rejects.toMatchObject({ name: 'AbortError' });
  });

  it("never starts from A's page once B's token is installed (the page not yet moved)", async () => {
    // A switch installed B's session; the profile request that moves the page is pending.
    client.setAccessToken(token('user-b', 900));
    await expect(
      client.uploadWithAuth(API, '/user/batch-files', new FormData(), AS_A),
    ).rejects.toMatchObject({ code: 'ACCOUNT_CHANGED' });
    expect(FakeXhr.sent).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('without a token, uploads only if the refresh names the expected account', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ access_token: token('user-b', 900) }), { status: 200 }),
    );
    await expect(
      client.uploadWithAuth(API, '/user/batch-files', new FormData(), AS_A),
    ).rejects.toMatchObject({ code: 'ACCOUNT_CHANGED' });
    expect(FakeXhr.sent).toEqual([]);

    vi.resetModules();
    client = await import('../client');
    sessionStorage.clear();
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ access_token: token('user-a', 900) }), { status: 200 }),
    );
    const resp = await client.uploadWithAuth(API, '/user/batch-files', new FormData(), AS_A);
    expect(resp.status).toBe(200);
    expect(FakeXhr.sent).toHaveLength(1);
  });

  it('never sends the upload again as the account installed meanwhile', async () => {
    client.setAccessToken(token('user-a', 900));
    FakeXhr.answers = [401];
    FakeXhr.beforeAnswer = () => client.setAccessToken(token('user-b', 900));
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ access_token: token('user-b', 900) }), { status: 200 }),
    );
    await expect(
      client.uploadWithAuth(API, '/user/batch-files', new FormData(), AS_A),
    ).rejects.toMatchObject({ code: 'ACCOUNT_CHANGED' });
    expect(FakeXhr.sent).toHaveLength(1);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('does not accept the answer after a switch away and back (A, B, A)', async () => {
    client.setAccessToken(token('user-a', 900));
    FakeXhr.answers = [200];
    FakeXhr.beforeAnswer = () => {
      client.setAccessToken(token('user-b', 900));
      client.setAccessToken(token('user-a', 900));
    };
    await expect(
      client.uploadWithAuth(API, '/user/batch-files', new FormData(), AS_A),
    ).rejects.toMatchObject({ code: 'ACCOUNT_CHANGED' });
  });
});

describe('a refresh that outlives its session (no Web Locks)', () => {
  it('installs nothing when it answers after another session was installed', async () => {
    client.setAccessToken(token('user-a', -60));
    const answer = deferred<Response>();
    fetchMock.mockReturnValueOnce(answer.promise);
    const request = client.fetchWithAuth(API, '/user/me');
    await Promise.resolve();
    const b = token('user-b', 900);
    client.setAccessToken(b);
    answer.resolve(
      new Response(JSON.stringify({ access_token: token('user-a', 900) }), { status: 200 }),
    );
    await expect(request).rejects.toMatchObject({ code: 'ACCOUNT_CHANGED' });
    expect(client.getAccessToken()).toBe(b);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does not end the new session on its late 401', async () => {
    client.setAccessToken(token('user-a', -60));
    const expired = vi.fn();
    window.addEventListener(client.AUTH_EXPIRED_EVENT, expired);
    const answer = deferred<Response>();
    fetchMock.mockReturnValueOnce(answer.promise);
    const request = client.fetchWithAuth(API, '/user/me');
    await Promise.resolve();
    const b = token('user-b', 900);
    client.setAccessToken(b);
    answer.resolve(new Response('{}', { status: 401 }));
    await expect(request).rejects.toMatchObject({ code: 'ACCOUNT_CHANGED' });
    window.removeEventListener(client.AUTH_EXPIRED_EVENT, expired);
    expect(client.getAccessToken()).toBe(b);
    expect(expired).not.toHaveBeenCalled();
  });

  it("does not refresh, and retry as, the new session after the old one's 401", async () => {
    client.setAccessToken(token('user-a', 900));
    const answer = deferred<Response>();
    fetchMock.mockReturnValueOnce(answer.promise);
    const request = client.fetchWithAuth(API, '/user/batches');
    await Promise.resolve();
    client.setAccessToken(token('user-b', 900));
    answer.resolve(new Response('{}', { status: 401 }));
    await expect(request).rejects.toMatchObject({ code: 'ACCOUNT_CHANGED' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('still refreshes the same session as before', async () => {
    client.setAccessToken(token('user-a', -60));
    const renewed = token('user-a', 900);
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ access_token: renewed }), { status: 200 }),
    );
    fetchMock.mockResolvedValueOnce(new Response('{}'));
    await client.fetchWithAuth(API, '/user/me');
    expect(client.getAccessToken()).toBe(renewed);
  });
});
