import { describe, expect, it } from 'vitest';
import { consoleWidthFor, isUserConsoleRoute, isWideConsoleRoute } from './consoleWidth';

describe('isWideConsoleRoute', () => {
  it('widens the admin area only', () => {
    expect(isWideConsoleRoute('/dashboard/admin')).toBe(true);
    expect(isWideConsoleRoute('/dashboard/admin/users')).toBe(true);
    expect(isWideConsoleRoute('/dashboard')).toBe(false);
    expect(isWideConsoleRoute('/dashboard/administrator')).toBe(false);
    expect(isWideConsoleRoute('/settings')).toBe(false);
  });
});

describe('isUserConsoleRoute', () => {
  it('covers the overview and its pages, not admin', () => {
    for (const path of [
      '/dashboard',
      '/dashboard/keys',
      '/dashboard/requests',
      '/dashboard/models',
      '/dashboard/batches/batch_1',
      '/dashboard/settings',
      '/dashboard/playground',
      '/dashboard/playground/x',
    ]) {
      expect(isUserConsoleRoute(path), path).toBe(true);
    }
    for (const path of ['/dashboard/admin', '/dashboard/admin/users', '/dashboards', '/settings']) {
      expect(isUserConsoleRoute(path), path).toBe(false);
    }
  });
});

describe('consoleWidthFor', () => {
  it('gives admin, the user console and the rest their own widths', () => {
    expect(consoleWidthFor('/dashboard/admin/users')).toBe('max-w-[1440px]');
    expect(consoleWidthFor('/dashboard/keys')).toBe('max-w-7xl');
    expect(consoleWidthFor('/login')).toBe('max-w-5xl');
  });
});
