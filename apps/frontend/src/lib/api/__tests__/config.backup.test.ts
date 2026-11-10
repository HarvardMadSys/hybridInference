import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { setAccessToken } from '../client';
import { exportConfigBackup, previewConfigRestore, restoreConfigBackup } from '../config';

const fetchMock = vi.fn();
const storage = new Map<string, string>();

// An unexpired token, so the request goes out without a refresh first.
const TOKEN = `header.${btoa(JSON.stringify({ sub: 'u1', exp: Math.floor(Date.now() / 1000) + 3600 }))}.signature`;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  storage.clear();
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('sessionStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  });
  setAccessToken(TOKEN);
});

afterEach(() => {
  setAccessToken(null);
  vi.unstubAllGlobals();
});

describe('exportConfigBackup', () => {
  it('reads the backup without secret values by default', async () => {
    fetchMock.mockResolvedValueOnce(json({ schema_version: 1, secrets_included: false }));

    await expect(exportConfigBackup()).resolves.toEqual({
      schema_version: 1,
      secrets_included: false,
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/admin\/config\/backup$/);
    expect(init.method ?? 'GET').toBe('GET');
    expect(init.body).toBeUndefined();
  });

  it('asks for secret values with the password, in the body', async () => {
    fetchMock.mockResolvedValueOnce(json({ schema_version: 1, secrets_included: true }));

    await exportConfigBackup({ includeSecrets: true, password: 'my-password' });

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/admin\/config\/backup$/);
    expect(String(url)).not.toContain('my-password');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ include_secrets: true, password: 'my-password' });
  });

  it('rejects with the gateway’s reason for a wrong password', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'Current password is incorrect.' }, 400));

    await expect(exportConfigBackup({ includeSecrets: true, password: 'wrong' })).rejects.toThrow(
      'Current password is incorrect.',
    );
  });
});

describe('restoring a backup', () => {
  const bundle = { schema_version: 1, app_config: [{ key: 'SITE_NAME', value: 'Lab' }] };

  it('previews the bundle as it is', async () => {
    fetchMock.mockResolvedValueOnce(
      json({ security_changes: ['app_config:ADMIN_EMAILS'], secrets_not_restored: [] }),
    );

    await expect(previewConfigRestore(bundle)).resolves.toEqual({
      security_changes: ['app_config:ADMIN_EMAILS'],
      secrets_not_restored: [],
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/admin\/config\/restore\/preview$/);
    expect(JSON.parse(init.body)).toEqual(bundle);
  });

  it('sends the confirmed changes and the password beside the bundle', async () => {
    fetchMock.mockResolvedValueOnce(
      json({ config: { entries: [] }, immutable_keys_preserved: [] }),
    );

    await restoreConfigBackup(bundle, {
      securityChanges: ['app_config:ADMIN_EMAILS'],
      password: 'my-password',
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/admin\/config\/restore$/);
    expect(JSON.parse(init.body)).toEqual({
      ...bundle,
      confirm_security_changes: ['app_config:ADMIN_EMAILS'],
      password: 'my-password',
    });
  });

  it('sends the bundle alone when there is nothing to confirm', async () => {
    fetchMock.mockResolvedValueOnce(
      json({ config: { entries: [] }, immutable_keys_preserved: [] }),
    );

    await restoreConfigBackup(bundle);

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(bundle);
  });

  it('never sends a confirmation the file itself carries', async () => {
    // A response body reads once: a fresh one for each request.
    fetchMock.mockImplementation(async () =>
      json({ config: { entries: [] }, immutable_keys_preserved: [] }),
    );
    const planted = {
      ...bundle,
      confirm_security_changes: ['app_config:ADMIN_EMAILS'],
      password: 'from-the-file',
    };

    await restoreConfigBackup(planted);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(bundle);

    await restoreConfigBackup(planted, { securityChanges: [], password: 'typed' });
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      ...bundle,
      confirm_security_changes: [],
      password: 'typed',
    });
  });
});
