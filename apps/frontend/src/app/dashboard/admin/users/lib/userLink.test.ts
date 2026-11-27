import { describe, expect, it } from 'vitest';
import { DEFAULT_FILTER_STATE, filterStateFromUrl } from './filterTypes';
import { OPEN_USER_PARAM, USERS_PATH, userDetailHref } from './userLink';

describe('userDetailHref', () => {
  it('lists the account by its id and names it to open', () => {
    const url = new URL(userDetailHref('01JABCDEFGHJKMNPQRSTVWXYZ0'), 'https://console.test');

    expect(url.pathname).toBe(USERS_PATH);
    // The Users tab reads the search back as it reads its own address.
    expect(filterStateFromUrl(url.searchParams)).toEqual({
      ...DEFAULT_FILTER_STATE,
      search: '01JABCDEFGHJKMNPQRSTVWXYZ0',
    });
    expect(url.searchParams.get(OPEN_USER_PARAM)).toBe('01JABCDEFGHJKMNPQRSTVWXYZ0');
  });

  it('keeps an id intact through the address', () => {
    const url = new URL(userDetailHref('a&b=c d'), 'https://console.test');

    expect(url.searchParams.get('q')).toBe('a&b=c d');
    expect(url.searchParams.get(OPEN_USER_PARAM)).toBe('a&b=c d');
  });
});
