// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ThemeProvider, useTheme } from './ThemeProvider';
import { THEME_BOOTSTRAP_SCRIPT, THEME_STORAGE_KEY } from './theme';

function Probe() {
  const { theme, resolvedTheme, setTheme } = useTheme();
  return (
    <>
      <output>
        {theme}:{resolvedTheme}
      </output>
      <button onClick={() => setTheme('light')}>Light</button>
      <button onClick={() => setTheme('dark')}>Dark</button>
      <button onClick={() => setTheme('system')}>System</button>
    </>
  );
}

describe('console appearance', () => {
  let query: EventTarget & { matches: boolean };

  beforeEach(() => {
    window.localStorage.clear();
    delete document.documentElement.dataset.consoleTheme;
    query = Object.assign(new EventTarget(), { matches: false });
    vi.stubGlobal(
      'matchMedia',
      vi.fn(() => query),
    );
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  function systemChanges(dark: boolean) {
    act(() => {
      query.matches = dark;
      query.dispatchEvent(new Event('change'));
    });
  }

  it('follows the OS by default without writing a preference', () => {
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    );
    expect(screen.getByText('system:light')).toBeInTheDocument();
    systemChanges(true);
    expect(screen.getByText('system:dark')).toBeInTheDocument();
    expect(document.documentElement.dataset.consoleTheme).toBe('dark');
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
  });

  it('restores an explicit choice and follows OS changes only after choosing System', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'dark');
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    );
    expect(screen.getByText('dark:dark')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Light'));
    systemChanges(true);
    expect(screen.getByText('light:light')).toBeInTheDocument();
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light');
    fireEvent.click(screen.getByText('System'));
    expect(screen.getByText('system:dark')).toBeInTheDocument();
    systemChanges(false);
    expect(screen.getByText('system:light')).toBeInTheDocument();
  });

  it('synchronizes another tab changing or clearing the preference', () => {
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    );
    act(() => {
      window.localStorage.setItem(THEME_STORAGE_KEY, 'dark');
      window.dispatchEvent(
        new StorageEvent('storage', { key: THEME_STORAGE_KEY, newValue: 'dark' }),
      );
    });
    expect(screen.getByText('dark:dark')).toBeInTheDocument();
    act(() => {
      window.localStorage.clear();
      window.dispatchEvent(new StorageEvent('storage', { key: null }));
    });
    expect(screen.getByText('system:light')).toBeInTheDocument();
  });

  it('keeps explicit choices working when storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    );
    fireEvent.click(screen.getByText('Light'));
    systemChanges(true);
    expect(screen.getByText('light:light')).toBeInTheDocument();
    fireEvent.click(screen.getByText('System'));
    expect(screen.getByText('system:dark')).toBeInTheDocument();
  });

  it('bootstraps dark before hydration without touching the document language or module classes', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'dark');
    document.documentElement.lang = 'zh-CN';
    document.documentElement.className = 'module-style';
    window.eval(THEME_BOOTSTRAP_SCRIPT);
    expect(document.documentElement.dataset.consoleTheme).toBe('dark');
    expect(document.documentElement.lang).toBe('zh-CN');
    expect(document.documentElement.className).toBe('module-style');
  });

  it('bootstraps the OS preference for invalid or inaccessible storage', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'invalid');
    query.matches = true;
    window.eval(THEME_BOOTSTRAP_SCRIPT);
    expect(document.documentElement.dataset.consoleTheme).toBe('dark');
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    query.matches = false;
    window.eval(THEME_BOOTSTRAP_SCRIPT);
    expect(document.documentElement.dataset.consoleTheme).toBe('light');
  });
});
