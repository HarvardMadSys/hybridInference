import { afterEach, describe, expect, it, vi } from 'vitest';

import { scopeProxiedSetCookie, withoutConsoleSession } from './consoleSessionCookie';

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('withoutConsoleSession', () => {
  it('drops the session cookie and keeps the rest', () => {
    expect(withoutConsoleSession('refresh_token=s; pga4_session=p')).toBe('pga4_session=p');
    expect(withoutConsoleSession('refresh_token=s')).toBeNull();
  });
});

describe('scopeProxiedSetCookie', () => {
  // pgAdmin and the agent app share the console's origin, so a cookie they set
  // under one of the console's names replaces the console's own.
  it('drops a cookie named like one the console owns', () => {
    for (const cookie of [
      'refresh_token=attacker; Path=/; HttpOnly',
      'REFRESH_TOKEN=attacker; Path=/auth',
      'refresh_token_device=x; Path=/auth',
      'hi_locale=zh-CN; Path=/',
      'refresh_token=; Max-Age=0; Path=/',
    ]) {
      expect(scopeProxiedSetCookie(cookie, '/pgadmin'), cookie).toBeNull();
    }
  });

  it('drops the deployment-scoped session and device cookies too', () => {
    vi.stubEnv('REFRESH_TOKEN_COOKIE_NAME', 'hybridinference_example_refresh');

    expect(scopeProxiedSetCookie('hybridinference_example_refresh=x', '/agents')).toBeNull();
    expect(scopeProxiedSetCookie('hybridinference_example_refresh_device=x', '/agents')).toBeNull();
    expect(scopeProxiedSetCookie('refresh_token=x', '/agents')).toBeNull();
  });

  it('drops a nameless cookie', () => {
    expect(scopeProxiedSetCookie('justavalue; Path=/', '/pgadmin')).toBeNull();
    expect(scopeProxiedSetCookie('=value; Path=/', '/pgadmin')).toBeNull();
  });

  it("narrows a cookie's path to the proxy prefix", () => {
    expect(
      scopeProxiedSetCookie('pga4_session=abc; Path=/; HttpOnly; SameSite=Lax', '/pgadmin'),
    ).toBe('pga4_session=abc; Path=/pgadmin; HttpOnly; SameSite=Lax');
    expect(scopeProxiedSetCookie('PGADMIN_LANGUAGE=en', '/pgadmin')).toBe(
      'PGADMIN_LANGUAGE=en; Path=/pgadmin',
    );
    expect(scopeProxiedSetCookie('x=1; Path=/auth', '/pgadmin')).toBe('x=1; Path=/pgadmin');
    expect(scopeProxiedSetCookie('x=1; Path=/pgadminx', '/pgadmin')).toBe('x=1; Path=/pgadmin');
  });

  it('keeps a path that already lies inside the prefix', () => {
    expect(scopeProxiedSetCookie('agent_session=one; Path=/agents; HttpOnly', '/agents')).toBe(
      'agent_session=one; Path=/agents; HttpOnly',
    );
    expect(scopeProxiedSetCookie('x=1; Path=/agents/jobs', '/agents')).toBe(
      'x=1; Path=/agents/jobs',
    );
  });

  it('applies the last Path, as a browser does', () => {
    expect(scopeProxiedSetCookie('x=1; Path=/agents; Path=/', '/agents')).toBe('x=1; Path=/agents');
  });

  it('removes Domain, so the cookie stays on this host', () => {
    expect(
      scopeProxiedSetCookie('x=1; Domain=.freeinference.org; Path=/pgadmin; Secure', '/pgadmin'),
    ).toBe('x=1; Path=/pgadmin; Secure');
  });

  // A browser rejects a __Host- cookie whose path is not `/`, so narrowing it
  // would sign the agent's users out instead of confining the cookie.
  it('leaves the path of a __Host- cookie alone', () => {
    expect(
      scopeProxiedSetCookie(
        '__Host-cloud_agent_session=agent; Path=/; Secure; HttpOnly; SameSite=Lax',
        '/agents',
      ),
    ).toBe('__Host-cloud_agent_session=agent; Path=/; Secure; HttpOnly; SameSite=Lax');
  });

  it('keeps expiry and the other attributes as they were', () => {
    expect(
      scopeProxiedSetCookie(
        'x=1; Expires=Wed, 21 Oct 2026 07:28:00 GMT; Max-Age=60; Secure; Partitioned',
        '/agents',
      ),
    ).toBe(
      'x=1; Path=/agents; Expires=Wed, 21 Oct 2026 07:28:00 GMT; Max-Age=60; Secure; Partitioned',
    );
  });
});
