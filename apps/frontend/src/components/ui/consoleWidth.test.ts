import { describe, expect, it } from 'vitest';
import { isWideConsoleRoute } from './consoleWidth';

describe('isWideConsoleRoute', () => {
  it('widens the admin area only', () => {
    expect(isWideConsoleRoute('/dashboard/admin')).toBe(true);
    expect(isWideConsoleRoute('/dashboard/admin/users')).toBe(true);
    expect(isWideConsoleRoute('/dashboard')).toBe(false);
    expect(isWideConsoleRoute('/dashboard/administrator')).toBe(false);
    expect(isWideConsoleRoute('/settings')).toBe(false);
  });
});
