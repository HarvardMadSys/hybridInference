import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { setAccessToken } from '../client';
import {
  ConfigVersionConflictError,
  getConfig,
  getLiveConfigStatus,
  markConfigSecret,
  patchConfig,
  resetConfigKey,
  type ConfigResponse,
} from '../config';
import { APIError } from '@/lib/utils/errors';

const fetchMock = vi.fn();

function makeFakeToken(): string {
  const header = btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }));
  const payload = btoa(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }));
  return `${header}.${payload}.sig`;
}

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

function config(revision: number): ConfigResponse {
  return {
    categories: [],
    entries: [],
    missing: [],
    pending_restart: [],
    restart_supported: false,
    source_version: { epoch: 'e1', revision },
  };
}

function ifMatchOf(call: number): string | null {
  return new Headers(fetchMock.mock.calls[call][1].headers).get('If-Match');
}

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

describe('conditional configuration writes', () => {
  it('sends exactly the version the caller names as If-Match', async () => {
    fetchMock.mockResolvedValueOnce(json(config(43)));
    await patchConfig(
      { values: { SITE_NAME: 'x' } },
      { expectedVersion: { epoch: 'e1', revision: 41 } },
    );
    expect(ifMatchOf(0)).toBe('"e1:41"');

    fetchMock.mockResolvedValueOnce(json(config(44)));
    await resetConfigKey('SITE_NAME', { expectedVersion: { epoch: 'e1', revision: 43 } });
    expect(ifMatchOf(1)).toBe('"e1:43"');

    fetchMock.mockResolvedValueOnce(json(config(45)));
    await markConfigSecret('CUSTOM_TOKEN', { expectedVersion: { epoch: 'e1', revision: 44 } });
    expect(ifMatchOf(2)).toBe('"e1:44"');
  });

  it('never takes a version from an unrelated read', async () => {
    // Another view, or a background refresh, reads a newer version...
    fetchMock.mockResolvedValueOnce(json(config(50)));
    await getConfig();
    // ...which must not become the base of a save edited on an older view.
    fetchMock.mockResolvedValueOnce(json(config(51)));
    await patchConfig(
      { values: { SITE_NAME: 'x' } },
      { expectedVersion: { epoch: 'e1', revision: 41 } },
    );
    expect(ifMatchOf(1)).toBe('"e1:41"');
    // Without a version (an older backend), nothing is conditional.
    fetchMock.mockResolvedValueOnce(json(config(52)));
    await patchConfig({ values: { SITE_NAME: 'y' } });
    expect(ifMatchOf(2)).toBeNull();
  });

  it('turns a version conflict into its own error', async () => {
    fetchMock.mockResolvedValueOnce(
      json({ detail: 'The configuration changed since it was loaded.' }, 409, {
        ETag: '"e1:45"',
      }),
    );
    const error = await patchConfig(
      { values: { SITE_NAME: 'x' } },
      { expectedVersion: { epoch: 'e1', revision: 41 } },
    ).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ConfigVersionConflictError);
    expect((error as APIError).code).toBe('CONFIG_VERSION_CONFLICT');
  });

  it('keeps an immutable setting refusal as a plain 409', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'API_KEY_SECRET cannot be changed.' }, 409));
    const error = await resetConfigKey('API_KEY_SECRET').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(APIError);
    expect(error).not.toBeInstanceOf(ConfigVersionConflictError);
  });

  it('reads the live status without sending a version', async () => {
    fetchMock.mockResolvedValueOnce(json({ database: false, groups: [], unknown_groups: [] }));
    const status = await getLiveConfigStatus();
    expect(status.database).toBe(false);
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/admin\/config\/live$/);
  });
});
