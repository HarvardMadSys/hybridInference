import { describe, expect, it } from 'vitest';

import { userAccountLabel, userAccountLabelKind, userDisplayName } from './userLabel';

describe('naming a user without an email', () => {
  const setupAdmin = { id: 'u1', email: null, login_name: 'admin', user_name: null };

  it('greets by display name, then email, then login name, then id', () => {
    expect(userDisplayName({ ...setupAdmin, user_name: 'Ops' })).toBe('Ops');
    expect(userDisplayName({ id: 'u2', email: 'a@example.test', user_name: '' })).toBe(
      'a@example.test',
    );
    expect(userDisplayName(setupAdmin)).toBe('admin');
    expect(userDisplayName({ id: 'u3', email: null, login_name: null })).toBe('u3');
    expect(userDisplayName(null)).toBe('');
  });

  it('identifies the account by email, then login name, then id — never the display name', () => {
    expect(userAccountLabel({ ...setupAdmin, user_name: 'Ops' })).toBe('admin');
    expect(userAccountLabel({ id: 'u2', email: 'a@example.test', login_name: 'a' })).toBe(
      'a@example.test',
    );
    expect(userAccountLabel({ user_id: 'u4', email: null })).toBe('u4');
    expect(userAccountLabelKind(setupAdmin)).toBe('login_name');
    expect(userAccountLabelKind({ email: 'a@example.test' })).toBe('email');
    expect(userAccountLabelKind({ id: 'u3' })).toBe('id');
  });
});
