import { describe, expect, it } from 'vitest';
import {
  consoleFrameFor,
  consoleWidthFor,
  isChatWorkspaceRoute,
  isDashboardRoute,
  isUserConsoleRoute,
  isWideConsoleRoute,
} from './consoleWidth';

describe('isChatWorkspaceRoute', () => {
  it('is Chat alone, including its trailing slash', () => {
    for (const path of ['/dashboard/playground', '/dashboard/playground/']) {
      expect(isChatWorkspaceRoute(path), path).toBe(true);
    }
    for (const path of [
      '/dashboard',
      '/dashboard/playground-other',
      '/dashboard/keys',
      '/dashboard/admin',
    ]) {
      expect(isChatWorkspaceRoute(path), path).toBe(false);
    }
  });
});

describe('isWideConsoleRoute', () => {
  it('widens the admin area and the organization console only', () => {
    expect(isWideConsoleRoute('/dashboard/admin')).toBe(true);
    expect(isWideConsoleRoute('/dashboard/admin/users')).toBe(true);
    expect(isWideConsoleRoute('/dashboard/organization')).toBe(true);
    expect(isWideConsoleRoute('/dashboard/organization/members')).toBe(true);
    expect(isWideConsoleRoute('/dashboard')).toBe(false);
    expect(isWideConsoleRoute('/dashboard/administrator')).toBe(false);
    expect(isWideConsoleRoute('/dashboard/organizations')).toBe(false);
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
    for (const path of [
      '/dashboard/admin',
      '/dashboard/admin/users',
      '/dashboard/organization/overview',
      '/dashboards',
      '/settings',
    ]) {
      expect(isUserConsoleRoute(path), path).toBe(false);
    }
  });
});

describe('isDashboardRoute', () => {
  it('covers the user console and the admin area, and nothing beside them', () => {
    for (const path of ['/dashboard', '/dashboard/keys', '/dashboard/admin/users']) {
      expect(isDashboardRoute(path), path).toBe(true);
    }
    for (const path of ['/', '/dashboards', '/login', '/stats']) {
      expect(isDashboardRoute(path), path).toBe(false);
    }
  });
});

describe('consoleWidthFor', () => {
  it('gives admin and the public pages a width, and leaves the user console to its shell', () => {
    expect(consoleWidthFor('/dashboard/admin/users')).toBe('max-w-[1440px]');
    // The organization console has the admin area's frame: its tabs, no navigation rail.
    expect(consoleWidthFor('/dashboard/organization/usage')).toBe('max-w-[1440px]');
    expect(consoleFrameFor('/dashboard/organization/usage')).toEqual({
      bar: true,
      shell: false,
      workspace: false,
    });
    expect(consoleWidthFor('/dashboard/keys')).toBe('max-w-none');
    expect(consoleWidthFor('/dashboard/playground')).toBe('max-w-none');
    expect(consoleWidthFor('/login')).toBe('max-w-5xl');
  });
});

describe('consoleFrameFor', () => {
  it('puts every user console page in the shell, and makes Chat alone a workspace', () => {
    expect(consoleFrameFor('/dashboard/keys')).toEqual({
      bar: true,
      shell: true,
      workspace: false,
    });
    expect(consoleFrameFor('/dashboard/playground')).toEqual({
      bar: true,
      shell: true,
      workspace: true,
    });
  });

  it('gives the admin area the bar without the shell, and a public page neither', () => {
    expect(consoleFrameFor('/dashboard/admin/users')).toEqual({
      bar: true,
      shell: false,
      workspace: false,
    });
    expect(consoleFrameFor('/login')).toEqual({ bar: false, shell: false, workspace: false });
  });
});
