// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { setAccessToken } from '../client';
import { exportOpenInfra, getOpenInfraDashboard, getOpenInfraHistory } from '../openinfra';

const fetchMock = vi.fn();

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

// A JWT-shaped token that won't expire during the run, so no refresh happens.
function makeFakeToken(): string {
  const header = btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }));
  const payload = btoa(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }));
  return `${header}.${payload}.sig`;
}

const DASHBOARD = {
  generated_at: 1_790_000_000_000,
  fleet: 'healthy',
  kpis: {},
  nodes: [],
  gpus: [],
  sglang: [],
  served: { hour: {}, day: {}, servers: [] },
  requests: [],
};
const HISTORY = { generated_at: 1_790_000_000_000, grain_ms: 60_000, gpu: [], sglang: [], cpu: [] };

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  setAccessToken(null);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('getOpenInfraDashboard', () => {
  it('reads the public snapshot without credentials', async () => {
    fetchMock.mockResolvedValueOnce(json(DASHBOARD));

    expect(await getOpenInfraDashboard()).toEqual({ status: 'ok', data: DASHBOARD });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/public-openinfra$/);
    expect(new Headers(init.headers).get('Authorization')).toBeNull();
    expect(init.credentials).toBeUndefined();
  });

  it('is unavailable when the deployment does not publish the page', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'Not Found' }, 404));
    expect(await getOpenInfraDashboard()).toEqual({ status: 'unavailable' });
  });

  it('is an error for any other failure, a refused connection or a document of another shape', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'down' }, 503));
    expect(await getOpenInfraDashboard()).toEqual({ status: 'error' });

    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    expect(await getOpenInfraDashboard()).toEqual({ status: 'error' });

    fetchMock.mockResolvedValueOnce(json({ ...DASHBOARD, sglang: null }));
    expect(await getOpenInfraDashboard()).toEqual({ status: 'error' });
  });
});

describe('getOpenInfraHistory', () => {
  it('asks for the range in hours', async () => {
    fetchMock.mockResolvedValueOnce(json(HISTORY));

    expect(await getOpenInfraHistory(24)).toEqual({ status: 'ok', data: HISTORY });
    const requested = new URL(String(fetchMock.mock.calls[0][0]), 'http://console.test');
    expect(requested.pathname).toBe('/public-openinfra/history');
    expect(requested.searchParams.get('hours')).toBe('24');
  });

  it('has the same three outcomes as the snapshot', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'Not Found' }, 404));
    expect(await getOpenInfraHistory(2)).toEqual({ status: 'unavailable' });

    fetchMock.mockResolvedValueOnce(json({ grain_ms: 60_000 }));
    expect(await getOpenInfraHistory(2)).toEqual({ status: 'error' });
  });
});

describe('exportOpenInfra', () => {
  it('downloads as the signed-in session, named as the gateway names it', async () => {
    setAccessToken(makeFakeToken());
    fetchMock.mockResolvedValueOnce(
      json({ days: 7 }, 200, {
        'Content-Disposition': 'attachment; filename="openinfra-export-7d-2026-11-28.json"',
      }),
    );

    const { filename, blob } = await exportOpenInfra(7);

    expect(filename).toBe('openinfra-export-7d-2026-11-28.json');
    expect(JSON.parse(await blob.text())).toEqual({ days: 7 });
    const [url, init] = fetchMock.mock.calls[0];
    const requested = new URL(String(url), 'http://console.test');
    expect(requested.pathname).toBe('/admin/openinfra/export');
    expect(requested.searchParams.get('days')).toBe('7');
    expect(new Headers(init.headers).get('Authorization')).toMatch(/^Bearer /);
    expect(init.credentials).toBe('include');
  });

  it('falls back to a name of its own when the header is hidden', async () => {
    setAccessToken(makeFakeToken());
    fetchMock.mockResolvedValueOnce(json({ days: 30 }));

    expect((await exportOpenInfra(30)).filename).toBe('openinfra-export-30d.json');
  });

  it('throws the gateway’s refusal', async () => {
    setAccessToken(makeFakeToken());
    fetchMock.mockResolvedValueOnce(
      json({ error: { type: 'forbidden', message: 'Admin access required.', code: 403 } }, 403),
    );

    await expect(exportOpenInfra(14)).rejects.toThrow('Admin access required.');
  });
});
