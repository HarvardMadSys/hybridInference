// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { PublicStats } from '@/lib/api/publicStats';

import StatsPage from './page';

// Charts are loaded with next/dynamic; jsdom cannot lay out recharts anyway.
vi.mock('next/dynamic', () => ({
  default: () => () => <div data-testid="chart" />,
}));

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useBranding: () => ({ appName: 'Example Inference' }),
}));

const weeks = ['2026-07-27', '2026-08-03'];
const stats: PublicStats = {
  schema_version: 1,
  generated_at: '2026-08-05T07:30:00+00:00',
  window: { start: '2026-07-31T07:33:00+00:00', end: '2026-08-05T07:00:00+00:00', days: 6 },
  weeks,
  first_week_partial: true,
  last_week_partial: true,
  totals: {
    tokens: 832_000_000_000,
    input_tokens: 828_000_000_000,
    output_tokens: 4_000_000_000,
    requests: 8_553_192,
    accounts: 471,
    cached_input_share: 0.9288,
  },
  registrations: { approved: 1466, waiting: 1066 },
  daily: [{ date: '2026-07-31', input_tokens: 10, output_tokens: 1, requests: 2 }],
  countries: {
    total: 83,
    total_min_requests: 71,
    continents: 6,
    weekly: [
      { any: 47, min_requests: 40 },
      { any: 55, min_requests: 48 },
    ],
    top: [{ code: 'USA', alpha2: 'US', share: 0.4506 }],
    all: [
      { code: 'USA', alpha2: 'US', continent: 'NA', level: 5 },
      { code: 'KEN', alpha2: 'KE', continent: 'AF', level: 1 },
    ],
  },
  languages: {
    total: 23,
    weekly: [11, 12],
    items: [
      { code: 'en', accounts: 393 },
      { code: 'lt', accounts: null },
    ],
    accounts_classified: 442,
    accounts_non_english: 181,
    messages_sampled: 16_598,
  },
  agents: {
    clients_total: 157,
    clients_multi_account: 53,
    products_total: 28,
    weekly: [
      { clients: 24, products: 16 },
      { clients: 41, products: 19 },
    ],
    products: [
      { name: 'Hermes Agent', kind: 'general', tokens: 118_000_000_000, accounts: 98 },
      { name: 'gptme', kind: 'coding', tokens: 1_700_000, accounts: null },
      { name: 'Cherry Studio', kind: 'chat', tokens: 7_700_000, accounts: 11 },
    ],
    kinds: [
      { kind: 'coding', token_share: 0.685 },
      { kind: 'general', token_share: 0.167 },
      { kind: 'custom', token_share: 0.025 },
      { kind: 'chat', token_share: 0.001 },
      { kind: 'direct', token_share: 0.122 },
    ],
    kind_weekly: weeks.map(() => ({ coding: 0.7, general: 0.2, custom: 0, chat: 0, direct: 0.1 })),
  },
  thresholds: { min_client_requests: 10, min_country_requests: 100, min_public_accounts: 3 },
};

const models: NonNullable<PublicStats['models']> = {
  items: [
    {
      name: 'minimax-m3',
      requests: 6_000_000,
      tokens: 200_000_000_000,
      daily_requests: [2],
      daily_tokens: [5],
    },
    {
      name: 'glm-5.1',
      requests: 2_000_000,
      tokens: 600_000_000_000,
      daily_requests: [1],
      daily_tokens: [11],
    },
  ],
  other: { requests: 553_192, tokens: 32_000_000_000, daily_requests: [1], daily_tokens: [1] },
};

function mockFetch(response: Partial<Response> & { json?: () => Promise<unknown> }) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, ...response }));
}

describe('StatsPage', () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('renders the headline figures and their breakdowns', async () => {
    mockFetch({ json: async () => stats });
    render(<StatsPage />);

    expect(await screen.findByText('832B')).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(
      'Example Inference usage stats',
    );
    expect(screen.getByText('83')).toBeInTheDocument();
    expect(screen.getByText('23')).toBeInTheDocument();
    expect(screen.getByText('157')).toBeInTheDocument();
    expect(screen.getByText('Approved users').nextElementSibling).toHaveTextContent('1,466');
    expect(screen.getByText('Waiting list').nextElementSibling).toHaveTextContent('1,066');
    expect(screen.getAllByText('United States').length).toBeGreaterThan(0);
    expect(screen.getByText('Hermes Agent')).toBeInTheDocument();
    // Chat apps are listed apart from the agent table.
    expect(screen.queryByRole('cell', { name: 'Cherry Studio' })).not.toBeInTheDocument();
    expect(screen.getByText(/1 chat app \(Cherry Studio\)/)).toBeInTheDocument();
    // Small account counts are withheld, not shown as numbers.
    expect(screen.getAllByText('<3').length).toBe(2);
    expect(fetch).toHaveBeenCalledWith(expect.stringMatching(/\/public-stats$/), expect.anything());
  });

  it('folds traffic by model until it is opened', async () => {
    mockFetch({ json: async () => ({ ...stats, models }) });
    render(<StatsPage />);

    const toggle = await screen.findByRole('button', { name: 'Traffic by model' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText(/glm-5\.1 served 72% of all tokens/)).toBeInTheDocument();
    expect(screen.queryByText('Other models')).not.toBeInTheDocument();
    expect(screen.getByText('Models.')).toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const body = within(document.getElementById('stats-models-body') as HTMLElement);
    // Busiest first by the chosen measure; the pooled rest always last.
    const names = () =>
      body
        .getAllByRole('row')
        .slice(1)
        .map((r) => r.firstChild?.textContent);
    expect(names()).toEqual(['glm-5.1', 'minimax-m3', 'Other models']);
    expect(body.getByRole('cell', { name: '600B' })).toBeInTheDocument();

    fireEvent.click(body.getByRole('button', { name: 'Requests' }));
    expect(names()).toEqual(['minimax-m3', 'glm-5.1', 'Other models']);
    expect(body.getByRole('cell', { name: '6M' })).toBeInTheDocument();

    fireEvent.focus(body.getByRole('img', { name: /^glm-5\.1, requests per day/ }));
    expect(body.getByRole('status')).toHaveTextContent('1 request');
    expect(body.getByRole('status')).toHaveTextContent('partial day');

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Other models')).not.toBeInTheDocument();
  });

  it('leaves out traffic by model for snapshots made before it was counted', async () => {
    mockFetch({ json: async () => stats });
    render(<StatsPage />);
    expect(await screen.findByText('832B')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Traffic by model' })).not.toBeInTheDocument();
    expect(screen.queryByText('Models.')).not.toBeInTheDocument();
  });

  it('says so when the deployment does not publish stats', async () => {
    mockFetch({ ok: false, status: 404 });
    render(<StatsPage />);
    expect(await screen.findByText(/not published for this site/)).toBeInTheDocument();
  });

  it('reports a failed load without breaking the page', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    render(<StatsPage />);
    expect(await screen.findByText(/could not be loaded/)).toBeInTheDocument();
  });

  it('leaves out account tiles for snapshots made before they were counted', async () => {
    const { registrations: _omitted, ...older } = stats;
    mockFetch({ json: async () => older });
    render(<StatsPage />);
    expect(await screen.findByText('832B')).toBeInTheDocument();
    expect(screen.queryByText('Approved users')).not.toBeInTheDocument();
    expect(screen.queryByText('Waiting list')).not.toBeInTheDocument();
    expect(screen.queryByText('Accounts.')).not.toBeInTheDocument();
  });

  it('says nobody is waiting when the waiting list is empty', async () => {
    mockFetch({ json: async () => ({ ...stats, registrations: { approved: 20, waiting: 0 } }) });
    render(<StatsPage />);
    expect(await screen.findByText('No sign-ups are waiting for review')).toBeInTheDocument();
  });

  it('omits the language section when the snapshot has none', async () => {
    mockFetch({ json: async () => ({ ...stats, languages: null }) });
    render(<StatsPage />);
    expect(await screen.findByText('Not measured on this deployment')).toBeInTheDocument();
    expect(
      screen.queryByRole('heading', { name: 'Languages people write in' }),
    ).not.toBeInTheDocument();
  });
});
