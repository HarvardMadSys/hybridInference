/**
 * How the console names a user, now that an email address is optional.
 *
 * The first-run administrator is created with a login name and no email, so
 * every place that printed `user.email` needs a fallback. There are two
 * questions and they have different answers:
 *
 * - **Who is this?** (a greeting, a header): the display name first —
 *   `user_name || email || login_name || id`.
 * - **Which account is this?** (an admin table row, a confirmation the admin
 *   has to type): the sign-in identifier — `email || login_name || id`. A
 *   display name is free-form and not unique, so it never stands in here.
 */

export interface UserIdentity {
  id?: string | null;
  user_id?: string | null;
  email?: string | null;
  login_name?: string | null;
  user_name?: string | null;
}

function firstPresent(...values: Array<string | null | undefined>): string {
  for (const value of values) {
    const trimmed = value?.trim();
    if (trimmed) return trimmed;
  }
  return '';
}

/** The name to show a person by: display name, email, login name, then id. */
export function userDisplayName(user: UserIdentity | null | undefined): string {
  if (!user) return '';
  return firstPresent(user.user_name, user.email, user.login_name, user.id, user.user_id);
}

/** The account's sign-in identifier: email, else login name, else id. */
export function userAccountLabel(user: UserIdentity | null | undefined): string {
  if (!user) return '';
  return firstPresent(user.email, user.login_name, user.id, user.user_id);
}

/** What `userAccountLabel` returned, so a prompt can say what to type. */
export type AccountLabelKind = 'email' | 'login_name' | 'id';

export function userAccountLabelKind(user: UserIdentity | null | undefined): AccountLabelKind {
  if (user?.email?.trim()) return 'email';
  if (user?.login_name?.trim()) return 'login_name';
  return 'id';
}
