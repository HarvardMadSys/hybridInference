import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { setAccessToken } from '../client';
import {
  checkBackendHealth,
  getConfig,
  markConfigSecret,
  patchConfig,
  resetConfigKey,
  restartBackend,
  testAlertNotification,
  waitForBackendRestart,
  type ConfigEntry,
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
  it('tests the selected saved alert channel with an authenticated POST and no secret', async () => {
    const result = { channel: 'wecom', sent: true, test_id: 'test-123' };
    fetchMock.mockResolvedValueOnce(json(result));

    await expect(testAlertNotification({ channel: 'wecom' })).resolves.toEqual(result);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/admin\/alerts\/test$/);
    expect(init.method).toBe('POST');
    expect(new Headers(init.headers).get('Authorization')).toMatch(/^Bearer /);
    expect(JSON.parse(init.body as string)).toEqual({ channel: 'wecom' });
  });

  it('tests an explicit draft webhook without writing configuration', async () => {
    fetchMock.mockResolvedValueOnce(json({ channel: 'slack', sent: true, test_id: 'test-456' }));
    const request = {
      channel: 'slack' as const,
      webhook_url: 'https://hooks.slack.com/services/test/example/token',
    };
    await testAlertNotification(request);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string)).toEqual(request);
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/admin\/alerts\/test$/);
  });

  it('rejects test delivery failures', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'Test notification delivery failed.' }, 502));
    await expect(testAlertNotification({ channel: 'slack' })).rejects.toThrow(
      'Test notification delivery failed.',
    );
  });

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
    fetchMock.mockResolvedValueOnce(json({ restarting: true, instance: 'abc123' }, 202));

    await expect(restartBackend()).resolves.toEqual({ restarting: true, instance: 'abc123' });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/admin\/system\/restart$/);
    expect(init.method).toBe('POST');
  });

  it('marks a stored variable secret with a PATCH that changes no value', async () => {
    fetchMock.mockResolvedValueOnce(json(EMPTY));

    await markConfigSecret('MY_ROUTE_HEADER');

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/admin\/config$/);
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(init.body as string)).toEqual({
      values: {},
      secrets: { MY_ROUTE_HEADER: true },
    });
  });

  it.each([
    'MY_KEY has no stored value to protect yet: set a value and mark it secret in the same save.',
    "SMTP_HOST: a built-in setting keeps the registry's secret flag.",
  ])('reports a refused flag-only change verbatim: %s', async (message) => {
    fetchMock.mockResolvedValueOnce(
      json({ error: { type: 'invalid_request_error', message, code: 400 } }, 400),
    );

    const error = await markConfigSecret('MY_KEY').catch((e: unknown) => e);

    expect(error).toBeInstanceOf(APIError);
    expect((error as APIError).statusCode).toBe(400);
    expect(getErrorMessage(error)).toBe(message);
  });

  it("keeps no secret's value or default, whatever the backend sent", async () => {
    const entry = (overrides: Partial<ConfigEntry>): ConfigEntry => ({
      key: 'SETTING',
      category: 'general',
      description: '',
      type: 'str',
      secret: false,
      required: false,
      missing: false,
      is_set: true,
      value: null,
      default: null,
      source: 'database',
      restart_required: false,
      pending_restart: false,
      environment_ignored: false,
      immutable: false,
      setup: false,
      custom: false,
      invalid: null,
      used_by: [],
      updated_at: null,
      updated_by: null,
      ...overrides,
    });
    const leaky: ConfigResponse = {
      ...EMPTY,
      entries: [
        entry({ key: 'SMTP_PASSWORD', secret: true, value: 'hunter2', default: 'changeme' }),
        entry({ key: 'SMTP_HOST', value: 'smtp.example.test', default: 'localhost' }),
      ],
    };

    for (const call of [
      () => getConfig(),
      () => patchConfig({ values: { SMTP_HOST: 'smtp.example.test' } }),
      () => markConfigSecret('SMTP_PASSWORD'),
      () => resetConfigKey('SMTP_HOST'),
    ]) {
      fetchMock.mockResolvedValueOnce(json(leaky));
      const [secret, plain] = (await call()).entries;

      expect(secret).toMatchObject({ key: 'SMTP_PASSWORD', value: null, default: null });
      expect(JSON.stringify(secret)).not.toMatch(/hunter2|changeme/);
      expect(plain).toMatchObject({ value: 'smtp.example.test', default: 'localhost' });
    }
  });

  // FastAPI's `{detail}`, and the gateway's envelope, which is what its
  // HTTPException handler answers in production.
  it.each([
    [400, 'SLACK_BOT_TOKEN: invalid value'],
    [403, 'DB_HOST is set in the environment only'],
    [409, 'API_KEY_SECRET cannot be changed once set'],
  ])('shows a %i message verbatim', async (status, message) => {
    for (const body of [
      { detail: message },
      { error: { type: 'invalid_request_error', message, code: status } },
    ]) {
      fetchMock.mockResolvedValueOnce(json(body, status));

      const error = await patchConfig({ values: { X: 'y' } }).catch((e: unknown) => e);

      expect(error).toBeInstanceOf(APIError);
      expect((error as APIError).statusCode).toBe(status);
      // "invalid … TOKEN" must not turn into "Invalid or expired token".
      expect(getErrorMessage(error)).toBe(message);
    }
  });
});

describe('restart polling', () => {
  const up = (instance: string | null = null) => ({ up: true, instance });
  const down = { up: false, instance: null };

  function sequence(...answers: { up: boolean; instance: string | null }[]) {
    return vi.fn(async () => answers.shift() ?? up());
  }

  it('waits for the health check to fail and then answer again', async () => {
    const check = sequence(up(), up(), down, down, up());

    const result = await waitForBackendRestart({ check, sleep: async () => undefined });

    expect(result).toBe('restarted');
    expect(check).toHaveBeenCalledTimes(5);
  });

  it('counts a different process answering as the restart, without seeing it down', async () => {
    // Back within one polling interval: every probe found it up.
    const check = sequence(up('old'), up('old'), up('new'));

    const result = await waitForBackendRestart({
      check,
      sleep: async () => undefined,
      progress: { sawDown: false, from: 'old' },
    });

    expect(result).toBe('restarted');
    expect(check).toHaveBeenCalledTimes(3);
  });

  it('does not count the same process answering after a failed probe', async () => {
    let clock = 0;
    const answers = [down, up('old'), up('old')];
    const check = vi.fn(async () => {
      clock += 1_000;
      return answers.shift() ?? up('old');
    });

    const result = await waitForBackendRestart({
      check,
      timeoutMs: 5_000,
      now: () => clock,
      sleep: async () => undefined,
      progress: { sawDown: false, from: 'old' },
    });

    expect(result).toBe('timeout');
  });

  it('falls back to seeing it down when the backend that comes back names no process', async () => {
    const check = sequence(up('old'), down, up());

    const result = await waitForBackendRestart({
      check,
      sleep: async () => undefined,
      progress: { sawDown: false, from: 'old' },
    });

    expect(result).toBe('restarted');
  });

  it('gives up at the deadline when the backend never comes back', async () => {
    let clock = 0;
    const check = vi.fn(async () => {
      clock += 1_000;
      return down;
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
      return up();
    });

    const result = await waitForBackendRestart({
      check,
      timeoutMs: 5_000,
      now: () => clock,
      sleep: async () => undefined,
    });

    expect(result).toBe('timeout');
  });

  it('resumes a timed-out wait for the same restart', async () => {
    // The first wait sees the outage but times out before the backend is back.
    let clock = 0;
    const progress = { sawDown: false };
    const failing = vi.fn(async () => {
      clock += 1_000;
      return down;
    });
    await expect(
      waitForBackendRestart({
        check: failing,
        timeoutMs: 3_000,
        now: () => clock,
        sleep: async () => undefined,
        progress,
      }),
    ).resolves.toBe('timeout');
    expect(progress.sawDown).toBe(true);

    // "Keep waiting": the backend already answers again, which is the restart.
    const answering = vi.fn(async () => up());
    await expect(
      waitForBackendRestart({ check: answering, sleep: async () => undefined, progress }),
    ).resolves.toBe('restarted');
    expect(answering).toHaveBeenCalledTimes(1);
  });

  it('stops when aborted', async () => {
    const controller = new AbortController();
    const check = vi.fn(async () => {
      controller.abort();
      return down;
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
    await expect(checkBackendHealth()).resolves.toEqual(down);

    fetchMock.mockResolvedValueOnce(new Response('bad gateway', { status: 502 }));
    await expect(checkBackendHealth()).resolves.toEqual(down);

    fetchMock.mockResolvedValueOnce(json({ status: 'healthy' }));
    await expect(checkBackendHealth()).resolves.toEqual(up());
    expect(String(fetchMock.mock.calls[2][0])).toMatch(/\/health$/);

    fetchMock.mockResolvedValueOnce(json({ status: 'healthy', instance: 'abc123' }));
    await expect(checkBackendHealth()).resolves.toEqual(up('abc123'));
  });
});
