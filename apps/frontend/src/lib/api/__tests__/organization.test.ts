import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getErrorMessage } from '@/lib/utils/errors';
import { setAccessToken } from '../client';
import {
  getOrganization,
  getOrganizationMember,
  getOrganizationOverview,
  listOrganizationAudit,
  listOrganizationMemberUsage,
  listOrganizationMembers,
  listOrganizationModelUsage,
  listOrganizationRequests,
  removeOrganizationMember,
  restoreOrganizationMember,
  setDefaultMemberLimit,
  setMemberMonthlyLimit,
} from '../organization';

const fetchMock = vi.fn();

function token(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `header.${btoa(JSON.stringify({ sub, exp }))}.signature`;
}

function respond(data: unknown, status = 200) {
  fetchMock.mockResolvedValueOnce(
    new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  );
}

function sent(call = 0): {
  path: string;
  query: URLSearchParams;
  options: RequestInit;
  body: unknown;
} {
  const [url, options] = fetchMock.mock.calls[call];
  const parsed = new URL(String(url), 'http://console.test');
  return {
    path: parsed.pathname,
    query: parsed.searchParams,
    options: options ?? {},
    body: typeof options?.body === 'string' ? JSON.parse(options.body) : undefined,
  };
}

let accessToken: string;

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('sessionStorage', {
    getItem: () => null,
    setItem: () => undefined,
    removeItem: () => undefined,
  });
  accessToken = token('administrator');
  setAccessToken(accessToken);
});

afterEach(() => {
  setAccessToken(null);
  vi.unstubAllGlobals();
});

describe('reading the organization', () => {
  it('asks for the organization of the caller, which no call names', async () => {
    respond({ id: 'o-1', name: 'Example University' });

    await expect(getOrganization()).resolves.toMatchObject({ id: 'o-1' });

    const request = sent();
    expect(request.path).toBe('/user/organization');
    expect([...request.query.keys()]).toEqual([]);
    expect(request.options.method ?? 'GET').toBe('GET');
    expect(new Headers(request.options.headers).get('Authorization')).toBe(`Bearer ${accessToken}`);
  });

  it('asks for the overview of a period', async () => {
    respond({ period: '30d', series: [], models: [] });

    await getOrganizationOverview('30d');

    expect(sent().path).toBe('/user/organization/overview');
    expect(sent().query.get('period')).toBe('30d');
  });

  it('sends the member filters under the names the gateway reads, and leaves out the unset ones', async () => {
    respond({ total: 0, members: [] });

    await listOrganizationMembers({
      period: '7d',
      search: 'wang',
      status: 'removed',
      sortBy: 'last_request',
      order: 'desc',
      limit: 50,
      offset: 100,
    });
    respond({ total: 0, members: [] });
    await listOrganizationMembers({ period: '1d' });

    expect(sent().path).toBe('/user/organization/members');
    expect(Object.fromEntries(sent().query)).toEqual({
      period: '7d',
      search: 'wang',
      status: 'removed',
      sort_by: 'last_request',
      order: 'desc',
      limit: '50',
      offset: '100',
    });
    expect(Object.fromEntries(sent(1).query)).toEqual({ period: '1d' });
  });

  it('asks for one member by its id, escaped', async () => {
    respond({ member: {}, days: [], models: [], requests: [] });

    await getOrganizationMember('u/1', '7d');

    expect(sent().path).toBe('/user/organization/members/u%2F1');
    expect(sent().query.get('period')).toBe('7d');
  });

  it('pages the requests with the cursor of the page before', async () => {
    respond({ requests: [], next_before: null });

    await listOrganizationRequests({
      period: '7d',
      model: 'glm-4.6',
      status: 'errors',
      limit: 50,
      before: '1795000000000000-42',
    });

    expect(sent().path).toBe('/user/organization/usage/requests');
    expect(Object.fromEntries(sent().query)).toEqual({
      period: '7d',
      model: 'glm-4.6',
      status: 'errors',
      limit: '50',
      before: '1795000000000000-42',
    });
  });

  it('unwraps the models of a period', async () => {
    respond({ period: '7d', models: [{ model_id: 'glm-4.6', requests: 3 }] });

    await expect(listOrganizationModelUsage('7d')).resolves.toEqual([
      { model_id: 'glm-4.6', requests: 3 },
    ]);
    expect(sent().path).toBe('/user/organization/usage/models');
  });

  it('asks for the usage of the members and for the audit log', async () => {
    respond({ total: 0, members: [] });
    await listOrganizationMemberUsage({ period: '30d', search: 'li', limit: 50, offset: 0 });
    respond({ total: 0, entries: [] });
    await listOrganizationAudit({ category: 'limits', search: 'lei', limit: 50, offset: 50 });

    expect(sent().path).toBe('/user/organization/usage/members');
    expect(Object.fromEntries(sent().query)).toEqual({
      period: '30d',
      search: 'li',
      limit: '50',
      offset: '0',
    });
    expect(sent(1).path).toBe('/user/organization/audit-log');
    // Without a period the log shows every row.
    expect(Object.fromEntries(sent(1).query)).toEqual({
      category: 'limits',
      search: 'lei',
      limit: '50',
      offset: '50',
    });
  });

  it('keeps the status of a refusal for the page to explain', async () => {
    respond({ error: { message: 'Organization administrator access required.', code: 403 } }, 403);

    await expect(getOrganization()).rejects.toMatchObject({ statusCode: 403 });
  });
});

describe('changing a limit', () => {
  it('sends a USD limit in the USD field', async () => {
    respond({ message: 'Monthly limit saved.' });

    await setMemberMonthlyLimit('u-2', { amount: 80, currency: 'USD' });

    const request = sent();
    expect(request.path).toBe('/user/organization/members/u-2/monthly-limit');
    expect(request.options.method).toBe('PUT');
    expect(request.body).toEqual({ monthly_limit_usd: 80 });
  });

  it('sends a native limit with its currency, never as dollars', async () => {
    respond({ message: 'Monthly limit saved.' });

    await setMemberMonthlyLimit('u-2', { amount: 500, currency: 'CNY' });

    expect(sent().body).toEqual({ monthly_limit: 500, currency: 'CNY' });
  });

  it('clears a limit with null, which is not the same as leaving it out', async () => {
    respond({ message: 'Monthly limit cleared.' });
    await setMemberMonthlyLimit('u-2', { amount: null, currency: 'USD' });
    respond({ id: 'o-1' });
    await setDefaultMemberLimit({ amount: null, currency: 'CNY' });

    expect(sent().body).toEqual({ monthly_limit_usd: null });
    expect(sent(1).path).toBe('/user/organization/default-member-limit');
    expect(sent(1).options.method).toBe('PUT');
    expect(sent(1).body).toEqual({ monthly_limit: null, currency: 'CNY' });
  });

  it('answers the default limit with the organization as it now is', async () => {
    respond({ id: 'o-1', default_member_monthly_limit_usd: 40 });

    await expect(setDefaultMemberLimit({ amount: 40, currency: 'USD' })).resolves.toMatchObject({
      default_member_monthly_limit_usd: 40,
    });
  });
});

describe('removing and restoring a member', () => {
  it('posts to the member, with no body', async () => {
    respond({ message: 'Member removed.' });
    await removeOrganizationMember('u-3');
    respond({ message: 'Member restored.' });
    await restoreOrganizationMember('u-3');

    expect(sent().path).toBe('/user/organization/members/u-3/remove');
    expect(sent().options.method).toBe('POST');
    expect(sent().options.body).toBeUndefined();
    expect(sent(1).path).toBe('/user/organization/members/u-3/restore');
    expect(sent(1).options.method).toBe('POST');
  });

  it('passes on what the gateway says when it refuses', async () => {
    respond({ error: { message: 'An administrator cannot be removed here.', code: 409 } }, 409);

    await expect(removeOrganizationMember('u-1')).rejects.toMatchObject({
      statusCode: 409,
      message: 'An administrator cannot be removed here.',
    });
  });
});

describe('a refusal of the gateway, as a page shows it', () => {
  /** The sentence a dialog shows for the failure of *call*. */
  async function shown(call: () => Promise<unknown>): Promise<string> {
    return getErrorMessage(await call().catch((error: unknown) => error));
  }

  function refuse(status: number, message: string) {
    respond({ error: { message, type: 'invalid_request_error', code: status } }, status);
  }

  it.each([
    [
      'a removal of a member that is no longer active',
      'Only an active member can be removed.',
      () => removeOrganizationMember('u-3'),
    ],
    [
      'a removal of an administrator',
      'An administrator cannot be removed here.',
      () => removeOrganizationMember('u-1'),
    ],
    [
      'a restore of a member that the platform suspended',
      'This member was not removed by the organization. A platform administrator restores an account they suspended.',
      () => restoreOrganizationMember('u-3'),
    ],
    [
      'a limit of an administrator',
      'The limit of an administrator is not set here.',
      () => setMemberMonthlyLimit('u-1', { amount: 40, currency: 'USD' }),
    ],
  ])('shows the sentence of the gateway for a 409: %s', async (_what, sentence, call) => {
    refuse(409, sentence);

    // Never the text of the sign-up page for a 409, "This email is already registered".
    await expect(shown(call)).resolves.toBe(sentence);
  });

  it('shows the sentence of the gateway for a limit above the limit of the organization', async () => {
    const sentence = "A member's limit cannot be greater than the limit of the organization.";
    refuse(400, sentence);

    await expect(
      shown(() => setDefaultMemberLimit({ amount: 5000, currency: 'USD' })),
    ).resolves.toBe(sentence);
  });

  it('shows the sentence of the gateway when a read is refused', async () => {
    refuse(403, 'Organization administrator access required.');

    await expect(shown(() => listOrganizationMembers({ period: '7d' }))).resolves.toBe(
      'Organization administrator access required.',
    );
  });

  it('says that the service is not available when the answer is not from the gateway', async () => {
    fetchMock.mockResolvedValueOnce(new Response('<html>Bad gateway</html>', { status: 502 }));

    await expect(shown(() => getOrganization())).resolves.toBe(
      'Service temporarily unavailable. Please try again shortly',
    );
  });
});
