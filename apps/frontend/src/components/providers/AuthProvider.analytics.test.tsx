// @vitest-environment jsdom
//
// A document running the analytics counter never becomes a signed-in one
// (src/components/analytics/PublicAnalytics.tsx). Its own file: the document's
// state lives in the API client's module, and these tests set it for good.
import '@testing-library/jest-dom/vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/auth', () => ({
  listAccounts: vi.fn().mockResolvedValue({ active_user_id: null, accounts: [], max_accounts: 5 }),
  endAgentSession: vi.fn(),
}));

import { keepDocumentSignedOut } from '@/lib/api/client';
import { AuthProvider, useAuth } from './AuthProvider';

const fetchMock = vi.fn();
const reload = vi.fn();

function Probe() {
  const { state } = useAuth();
  return (
    <span>{state.loading ? 'loading' : state.isAuthenticated ? 'signed in' : 'signed out'}</span>
  );
}

async function renderProvider() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthProvider>
        <Probe />
      </AuthProvider>
    </QueryClientProvider>,
  );
  expect(await screen.findByText('signed out')).toBeInTheDocument();
}

beforeEach(() => {
  fetchMock.mockReset().mockResolvedValue(new Response('{}', { status: 200 }));
  reload.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('location', { ...window.location, reload });
  keepDocumentSignedOut();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('AuthProvider in a document kept signed out', () => {
  // SameSite=Strict: a visitor arriving from another site renders signed out,
  // although the browser holds a session cookie a same-origin refresh would use.
  it('never looks for a session: nothing is refreshed', async () => {
    await renderProvider();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('takes up an account signed in from another tab by reloading, not in place', async () => {
    await renderProvider();

    const tab = new BroadcastChannel('hybridinference:accounts');
    tab.postMessage({ type: 'active', userId: 'new-user' });
    tab.close();

    await waitFor(() => expect(reload).toHaveBeenCalledOnce());
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByText('signed out')).toBeInTheDocument();
  });
});
