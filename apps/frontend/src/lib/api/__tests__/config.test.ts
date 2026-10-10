import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { setAccessToken } from '../client';
import {
  checkBackendHealth,
  getConfig,
  patchConfig,
  resetConfigKey,
  restartBackend,
  waitForBackendRestart,
  type ConfigResponse,
} from '../config';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

const fetchMock = vi.fn();

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

const EMPTY: ConfigResponse = {
  categories: [],
  entries: [],
  missing: [],
  pending_restart: [],
  restart_supported: true,
};

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('sessionStorage', {
    getItem: () => null,
    setItem: () => undefined,
    removeItem: () => undefined,
  });
  setAccessToken(makeFakeToken());
});

afterEach(() => {
  setAccessToken(null);
  vi.unstubAllGlobals();
});

describe('configuration client', () => {
  it('reads GET /admin/config with the session', async () => {
    fetchMock.mockResolvedValueOnce(json(EMPTY));

    await expect(getConfig()).resolves.toEqual(EMPTY);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/admin\/config$/);
    expect(new Headers(init.headers).get('Authorization')).toMatch(/^Bearer /);
  });

  it('sends typed values in one PATCH', async () => {
    fetchMock.mockResolvedValueOnce(json(EMPTY));

    await patchConfig({
      values: { SMTP_PORT: 587, TRUST_PROXY_HEADERS: true, SMTP_HOST: 'smtp.example.test' },
      secrets: { CUSTOM_KEY: true },
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/admin\/config$/);
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(init.body as string)).toEqual({
      values: { SMTP_PORT: 587, TRUST_PROXY_HEADERS: true, SMTP_HOST: 'smtp.example.test' },
      secrets: { CUSTOM_KEY: true },
    });
  });

  it('resets one key with DELETE', async () => {
    fetchMock.mockResolvedValueOnce(json(EMPTY));

    await resetConfigKey('SMTP_PASSWORD');

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/admin\/config\/SMTP_PASSWORD$/);
    expect(init.method).toBe('DELETE');
  });

  it('asks for a restart with POST /admin/system/restart', async () => {
    fetchMock.mockResolvedValueOnce(json({ restarting: true }, 202));

    await expect(restartBackend()).resolves.toEqual({ restarting: true });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/admin\/system\/restart$/);
    expect(init.method).toBe('POST');
  });

  it.each([
    [400, 'SLACK_BOT_TOKEN: invalid value'],
    [403, 'DB_HOST is set in the environment only'],
    [409, 'API_KEY_SECRET cannot be changed once set'],
  ])('shows a %i detail verbatim', async (status, detail) => {
    fetchMock.mockResolvedValueOnce(json({ detail }, status));

    const error = await patchConfig({ values: { X: 'y' } }).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(APIError);
    expect((error as APIError).statusCode).toBe(status);
    // "invalid … TOKEN" must not turn into "Invalid or expired token".
    expect(getErrorMessage(error)).toBe(detail);
  });
});

describe('restart polling', () => {
  function sequence(...answers: boolean[]) {
    const check = vi.fn(async () => answers.shift() ?? true);
    return check;
  }

  it('waits for the health check to fail and then answer again', async () => {
    const check = sequence(true, true, false, false, true);

    const result = await waitForBackendRestart({ check, sleep: async () => undefined });

    expect(result).toBe('restarted');
    expect(check).toHaveBeenCalledTimes(5);
  });

  it('gives up at the deadline when the backend never comes back', async () => {
    let clock = 0;
    const check = vi.fn(async () => {
      clock += 1_000;
      return false;
    });

    const result = await waitForBackendRestart({
      check,
      timeoutMs: 10_000,
      now: () => clock,
      sleep: async () => undefined,
    });

    expect(result).toBe('timeout');
  });

  it('does not count an answer before the outage as the restart', async () => {
    let clock = 0;
    const check = vi.fn(async () => {
      clock += 1_000;
      return true;
    });

    const result = await waitForBackendRestart({
      check,
      timeoutMs: 5_000,
      now: () => clock,
      sleep: async () => undefined,
    });

    expect(result).toBe('timeout');
  });

  it('stops when aborted', async () => {
    const controller = new AbortController();
    const check = vi.fn(async () => {
      controller.abort();
      return false;
    });

    const result = await waitForBackendRestart({
      check,
      signal: controller.signal,
      sleep: async () => undefined,
    });

    expect(result).toBe('aborted');
  });

  it('counts a refused connection or an error status as down', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('connection refused'));
    await expect(checkBackendHealth()).resolves.toBe(false);

    fetchMock.mockResolvedValueOnce(new Response('bad gateway', { status: 502 }));
    await expect(checkBackendHealth()).resolves.toBe(false);

    fetchMock.mockResolvedValueOnce(json({ status: 'healthy' }));
    await expect(checkBackendHealth()).resolves.toBe(true);
    expect(String(fetchMock.mock.calls[2][0])).toMatch(/\/health$/);
  });
});
