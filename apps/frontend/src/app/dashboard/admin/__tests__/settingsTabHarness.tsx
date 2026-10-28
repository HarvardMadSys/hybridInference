import type { ComponentProps } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import type { ConfigResponse } from '@/lib/api/config';

import { SettingsTab } from '../SettingsTab';

/** A configuration with nothing in it, for tests about the page's other sections. */
export const EMPTY_CONFIG: ConfigResponse = {
  categories: [],
  entries: [],
  missing: [],
  pending_restart: [],
  restart_supported: false,
};

/** Open a sub-tab of Admin → Settings by its name, once the page shows it. */
export async function openSettingsTab(name: string | RegExp) {
  fireEvent.click(await screen.findByRole('tab', { name }));
}

/** Render Admin → Settings, then open the sub-tab named `tab`, if any. */
export async function renderSettings(tab?: string, props: ComponentProps<typeof SettingsTab> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <SettingsTab {...props} />
    </QueryClientProvider>,
  );
  if (tab) await openSettingsTab(tab);
  return view;
}
