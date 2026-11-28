// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { RoutingTab } from './RoutingTab';

vi.mock('./ProviderRoutesTab', () => ({
  ProviderRoutesTab: vi.fn(() => <div data-testid="provider-routes-tab">provider routes</div>),
}));

describe('RoutingTab', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders the top-level routing control page', () => {
    render(<RoutingTab />);

    expect(screen.getByRole('heading', { level: 2, name: 'Routing' })).toBeInTheDocument();
    expect(screen.getByTestId('provider-routes-tab')).toHaveTextContent('provider routes');
    expect(
      screen.getByText(/RouteWise parameters are under Settings → Providers & Routing/),
    ).toBeInTheDocument();
  });
});
