// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { exportRequests } from '../admin';
import { setAccessToken } from '../client';

const fetchMock = vi.fn();
const LINK = '/admin/export/requests/download?token=signed';

// A JWT-shaped token that won't expire during the run, so no refresh happens.
function makeFakeToken(): string {
  const header = btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }));
  const payload = btoa(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }));
  return `${header}.${payload}.sig`;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

let clicked: HTMLAnchorElement[];

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  setAccessToken(makeFakeToken());
  clicked = [];
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    clicked.push(this);
  });
});

afterEach(() => {
  setAccessToken(null);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('exportRequests', () => {
  it('asks for a download link and hands it to the browser to download', async () => {
    fetchMock.mockResolvedValueOnce(json({ url: LINK, expires_in: 60 }));

    await exportRequests({
      startTime: '2026-09-01T00:00:00.000Z',
      endTime: '2026-11-14T23:59:59.000Z',
      userId: 'SpiritedAwayCN',
      includeContent: true,
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    const requested = new URL(String(url), 'http://console.test');
    expect(requested.pathname).toBe('/admin/export/requests/link');
    expect(init.method).toBe('POST');
    expect(new Headers(init.headers).get('Authorization')).toMatch(/^Bearer /);
    expect(Object.fromEntries(requested.searchParams)).toEqual({
      start_time: '2026-09-01T00:00:00.000Z',
      end_time: '2026-11-14T23:59:59.000Z',
      user_id: 'SpiritedAwayCN',
      include_content: 'true',
    });

    // The browser follows the link itself: one anchor, to the gateway, as a download.
    expect(clicked).toHaveLength(1);
    expect(clicked[0].getAttribute('href')).toMatch(
      /\/admin\/export\/requests\/download\?token=signed$/,
    );
    expect(clicked[0].hasAttribute('download')).toBe(true);
    expect(document.querySelector('a[download]')).toBeNull();
  });

  it('reports why a link was refused and starts no download', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'Admin access required.' }, 403));

    await expect(exportRequests({ startTime: '2026-09-01T00:00:00.000Z' })).rejects.toThrow(
      'Admin access required.',
    );
    expect(clicked).toHaveLength(0);
  });
});
