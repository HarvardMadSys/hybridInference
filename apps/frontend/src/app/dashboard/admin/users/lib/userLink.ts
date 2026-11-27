import { DEFAULT_FILTER_STATE, filterStateToUrl } from './filterTypes';

/** The admin Users tab. */
export const USERS_PATH = '/dashboard/admin/users';

/** The address parameter naming an account whose details the Users tab opens on arrival. */
export const OPEN_USER_PARAM = 'open';

/**
 * The Users tab showing one account with its details open: where a user named
 * elsewhere in the console (a row of Recent Requests) links to. The search
 * matches a user-id prefix, so the whole id lists that account.
 */
export function userDetailHref(userId: string): string {
  const params = new URLSearchParams(filterStateToUrl({ ...DEFAULT_FILTER_STATE, search: userId }));
  params.set(OPEN_USER_PARAM, userId);
  return `${USERS_PATH}?${params.toString()}`;
}
