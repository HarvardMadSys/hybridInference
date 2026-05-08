// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ProviderKeysTab } from '../ProviderKeysTab';

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

vi.mock('@/lib/api/admin', () => ({
  addProviderKey: vi.fn(async () => ({ pools_updated: 0 })),
  deleteProviderKey: vi.fn(),
  getProviderQuotas: vi.fn(async () => ({
    generated_at: '2026-05-07T00:00:00Z',
    providers: [
      {
        name: 'chatgpt',
        display_name: 'ChatGPT',
        key_configured: false,
        key_masked: null,
        fetched_at: '2026-05-07T00:00:00Z',
        ok: false,
        error: 'not_configured',
        usages: [],
      },
      {
        name: 'openrouter',
        display_name: 'OpenRouter',
        key_configured: false,
        key_masked: null,
        fetched_at: '2026-05-07T00:00:00Z',
        ok: false,
        error: 'not_configured',
        usages: [],
      },
    ],
  })),
  listProviderKeys: vi.fn(async () => ({ provider: 'chatgpt', keys: [] })),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ProviderKeysTab cookie upload', () => {
  it('shows ChatGPT upload action when ChatGPT is the selected provider', async () => {
    render(<ProviderKeysTab />);

    expect(await screen.findByRole('button', { name: /upload cookie/i })).toBeInTheDocument();
  });

  it('hides upload action for non-ChatGPT providers', async () => {
    render(<ProviderKeysTab />);

    await screen.findByRole('button', { name: /upload cookie/i });
    fireEvent.change(screen.getByLabelText(/^provider$/i), { target: { value: 'openrouter' } });

    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /upload cookie/i })).not.toBeInTheDocument();
    });
  });

  it('normalizes a Cookie header and submits through addProviderKey', async () => {
    const api = await import('@/lib/api/admin');
    render(<ProviderKeysTab />);

    fireEvent.click(await screen.findByRole('button', { name: /upload cookie/i }));
    fireEvent.change(screen.getByLabelText(/cookie input/i), {
      target: {
        value: 'Cookie: foo=bar; __Secure-next-auth.session-token=abc123; cf_clearance=clear456',
      },
    });
    fireEvent.change(screen.getByLabelText(/cookie label/i), {
      target: { value: 'team account' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save cookie/i }));

    await waitFor(() => {
      expect(api.addProviderKey).toHaveBeenCalledWith(
        'chatgpt',
        '__Secure-next-auth.session-token=abc123; cf_clearance=clear456',
        'team account',
      );
    });
  });

  it('submits trimmed raw cookie input', async () => {
    const api = await import('@/lib/api/admin');
    render(<ProviderKeysTab />);

    fireEvent.click(await screen.findByRole('button', { name: /upload cookie/i }));
    fireEvent.change(screen.getByLabelText(/cookie input/i), {
      target: { value: '  session=raw_cookie_value  ' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save cookie/i }));

    await waitFor(() => {
      expect(api.addProviderKey).toHaveBeenCalledWith(
        'chatgpt',
        'session=raw_cookie_value',
        undefined,
      );
    });
  });

  it('shows validation error and does not submit invalid Cookie header', async () => {
    const api = await import('@/lib/api/admin');
    render(<ProviderKeysTab />);

    fireEvent.click(await screen.findByRole('button', { name: /upload cookie/i }));
    fireEvent.change(screen.getByLabelText(/cookie input/i), {
      target: { value: 'Cookie: foo=bar' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save cookie/i }));

    expect(await screen.findByText(/did not include a session cookie/i)).toBeInTheDocument();
    expect(api.addProviderKey).not.toHaveBeenCalled();
  });

  it('keeps cookie upload outside the generic add-key form', async () => {
    const api = await import('@/lib/api/admin');
    render(<ProviderKeysTab />);

    fireEvent.click(await screen.findByRole('button', { name: /upload cookie/i }));
    fireEvent.change(screen.getByLabelText(/^api key$/i), {
      target: { value: 'generic-key-should-not-submit' },
    });
    const cookieLabelInput = screen.getByLabelText(/cookie label/i);
    fireEvent.change(cookieLabelInput, { target: { value: 'team account' } });
    fireEvent.keyDown(cookieLabelInput, { key: 'Enter', code: 'Enter' });

    expect(api.addProviderKey).not.toHaveBeenCalled();

    const genericForm = screen.getByRole('form', { name: /add a new key/i });
    const cookieInput = screen.getByLabelText(/cookie input/i);
    expect(genericForm).not.toContainElement(cookieInput);
  });
});
