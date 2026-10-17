// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import toast from 'react-hot-toast';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider, clearToasts } from './ToastProvider';

// jsdom has no matchMedia. react-hot-toast asks it about reduced motion, and
// the provider asks it how wide the screen is; this answers as a screen of
// `width` pixels, and `resize` moves it across breakpoints.
function screenOf(width: number) {
  let current = width;
  const listeners = new Set<() => void>();
  vi.stubGlobal('matchMedia', (query: string) => {
    const minWidth = /min-width:\s*(\d+)px/.exec(query);
    return {
      media: query,
      matches: minWidth ? current >= Number(minWidth[1]) : false,
      addEventListener: (_type: string, listener: () => void) => listeners.add(listener),
      removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener),
    };
  });
  return {
    resize(next: number) {
      current = next;
      act(() => listeners.forEach((listener) => listener()));
    },
  };
}

function show(raise: () => void) {
  render(<ToastProvider />);
  act(() => {
    raise();
  });
}

// The box react-hot-toast positions a toast in, inside its fixed full-screen layer.
function slotOf(text: string): HTMLElement {
  const slot = screen.getByText(text).closest<HTMLElement>('[data-rht-toaster] > div');
  if (!slot) throw new Error(`No toast slot holds "${text}"`);
  return slot;
}

afterEach(() => {
  cleanup();
  // The toasts live in react-hot-toast's module store, not in the unmounted toaster.
  toast.remove();
  vi.unstubAllGlobals();
});

describe('ToastProvider placement', () => {
  // The header's account menu is in the top-right corner, and a toast there
  // took the clicks meant for it after every sign-in and switch.
  it('puts toasts in the bottom-right corner on a desktop, clear of the header', () => {
    screenOf(1280);
    show(() => toast.success('Switched to Ada'));

    const slot = slotOf('Switched to Ada');
    expect(slot).toHaveStyle({ bottom: '0px', justifyContent: 'flex-end' });
    expect(slot.style.top).toBe('');
  });

  it('centres them along the bottom on a phone', () => {
    screenOf(375);
    show(() => toast.success('Switched to Ada'));

    const slot = slotOf('Switched to Ada');
    expect(slot).toHaveStyle({ bottom: '0px', justifyContent: 'center' });
    expect(slot.style.top).toBe('');
  });

  it('follows the screen across the phone breakpoint', () => {
    const display = screenOf(1280);
    show(() => toast('Saved'));
    expect(slotOf('Saved')).toHaveStyle({ justifyContent: 'flex-end' });

    display.resize(375);

    expect(slotOf('Saved')).toHaveStyle({ bottom: '0px', justifyContent: 'center' });
  });
});

describe('ToastProvider toasts', () => {
  it('announces a toast politely and lets the visitor dismiss it', () => {
    screenOf(1280);
    show(() => toast.error('Could not save the key'));

    const status = screen.getByRole('status');
    expect(status).toHaveTextContent('Could not save the key');
    expect(status).toHaveAttribute('aria-live', 'polite');

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss notification' }));

    expect(screen.queryByText('Could not save the key')).toBeNull();
  });

  it('clearToasts takes every toast off the screen at once', () => {
    screenOf(1280);
    show(() => {
      toast.success('Reset OPENROUTER_API_KEY.');
      toast.error('Quota exceeded');
    });
    expect(screen.getAllByRole('status')).toHaveLength(2);

    act(() => clearToasts());

    // Gone, not fading out: an exiting toast is still readable.
    expect(screen.queryAllByRole('status')).toHaveLength(0);
  });
});
