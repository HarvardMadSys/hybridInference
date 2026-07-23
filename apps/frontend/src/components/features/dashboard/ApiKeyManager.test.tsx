// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiKeyListItem } from '@/lib/api/user';
import { ApiKeyManager } from './ApiKeyManager';

vi.mock('@/lib/hooks', () => ({
  useApiKeys: vi.fn(),
  useCreateApiKey: vi.fn(),
  useDeleteApiKey: vi.fn(),
  useRegenerateApiKey: vi.fn(),
}));

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useBranding: () => ({ appName: 'Test App' }),
}));

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

import { useApiKeys, useCreateApiKey, useDeleteApiKey, useRegenerateApiKey } from '@/lib/hooks';

const idleMutation = { mutateAsync: vi.fn(), isPending: false };

function mockList(item: ApiKeyListItem): void {
  vi.mocked(useApiKeys).mockReturnValue({
    data: { keys: [item] },
    isLoading: false,
    error: null,
  } as ReturnType<typeof useApiKeys>);
  vi.mocked(useCreateApiKey).mockReturnValue(
    idleMutation as unknown as ReturnType<typeof useCreateApiKey>,
  );
  vi.mocked(useDeleteApiKey).mockReturnValue(
    idleMutation as unknown as ReturnType<typeof useDeleteApiKey>,
  );
  vi.mocked(useRegenerateApiKey).mockReturnValue(
    idleMutation as unknown as ReturnType<typeof useRegenerateApiKey>,
  );
}

const FULL_KEY = 'hyi-testkey01-FULL-SECRET-VALUE';
const MASKED = 'hyi-testkey01********************';

function makeItem(overrides: Partial<ApiKeyListItem> = {}): ApiKeyListItem {
  return {
    api_key: FULL_KEY,
    key_prefix: 'hyi-testkey01',
    key_masked: MASKED,
    created_at: '2026-05-06T12:00:00.000Z',
    last_used_at: null,
    status: 'active',
    ...overrides,
  };
}

let writeText: ReturnType<typeof vi.fn>;

beforeEach(() => {
  writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ApiKeyManager reveal + copy', () => {
  it('masks a revealable key by default and shows the full key after toggling', () => {
    mockList(makeItem());
    render(<ApiKeyManager />);

    // Masked value is shown; the full key is hidden until the user reveals it.
    expect(screen.getByText(MASKED)).toBeInTheDocument();
    expect(screen.queryByText(FULL_KEY)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Show key' }));

    // Full key is now visible and the toggle flips to "Hide key".
    expect(screen.getByText(FULL_KEY)).toBeInTheDocument();
    expect(screen.queryByText(MASKED)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Hide key' })).toBeInTheDocument();
  });

  it('copies the full key to the clipboard', () => {
    mockList(makeItem());
    render(<ApiKeyManager />);

    fireEvent.click(screen.getByRole('button', { name: 'Copy key' }));

    expect(writeText).toHaveBeenCalledWith(FULL_KEY);
  });

  it('does not render reveal/copy controls for a legacy key without api_key', () => {
    mockList(makeItem({ api_key: null }));
    render(<ApiKeyManager />);

    expect(screen.getByText(MASKED)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Show key' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Copy key' })).not.toBeInTheDocument();
    expect(
      screen.getByText(/Full key is only shown immediately after creation or regeneration/),
    ).toBeInTheDocument();
  });
});
