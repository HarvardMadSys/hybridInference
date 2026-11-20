// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import type { OrganizationOverview } from '@/lib/api/organization';

// The chart is loaded with next/dynamic; jsdom cannot lay out recharts anyway.
// `data-values` is each value as the chart writes it in its tooltip, and
// `data-ticks` as it writes it on its axis.
vi.mock('next/dynamic', () => ({
  default:
    () =>
    (props: {
      kind: string;
      points: Array<{ label: string; value: number }>;
      formatValue: (value: number) => string;
      formatTick?: (value: number) => string;
    }) => (
      <div
        data-testid="chart"
        data-kind={props.kind}
        data-values={props.points.map((point) => props.formatValue(point.value)).join(' ')}
        data-ticks={props.points
          .map((point) => (props.formatTick ?? props.formatValue)(point.value))
          .join(' ')}
      >
        {props.points.map((point) => `${point.label}=${point.value}`).join(' ')}
      </div>
    ),
}));

const state = vi.hoisted(() => ({ overview: vi.fn(), refetch: vi.fn() }));
vi.mock('@/lib/hooks', () => ({
  useOrganizationOverview: (period: string) => state.overview(period),
}));

import { OverviewView } from './OverviewView';

function day(date: string, cost: number, members: number) {
  return {
    bucket: `${date}T00:00:00Z`,
    cost_usd: cost,
    requests: members * 10,
    total_tokens: members * 1000,
    members_with_requests: members,
  };
}

const WEEK: OrganizationOverview = {
  period: '7d',
  bucket: 'day',
  cost_usd: 121.76,
  requests: 4464,
  total_tokens: 18_040_000,
  members_with_requests: 22,
  member_count: 24,
  previous: { cost_usd: 129.53, requests: 4602 },
  spent_month_usd: 298.23,
  monthly_limit_usd: 1000,
  series: [day('2026-11-16', 22.5, 16), day('2026-11-17', 25.94, 15)],
  models: [
    {
      model_id: 'deepseek-v3.2',
      requests: 900,
      errors: 9,
      prompt_tokens: 1,
      completion_tokens: 1,
      cost_usd: 32.87,
    },
    {
      model_id: 'glm-4.6',
      requests: 1600,
      errors: 20,
      prompt_tokens: 1,
      completion_tokens: 1,
      cost_usd: 49.92,
    },
    {
      model_id: 'free-model',
      requests: 40,
      errors: 0,
      prompt_tokens: 1,
      completion_tokens: 1,
      cost_usd: 0,
    },
  ],
};

function answer(data: OrganizationOverview | undefined, more: Record<string, unknown> = {}) {
  state.overview.mockReturnValue({
    data,
    error: null,
    isLoading: false,
    isPlaceholderData: false,
    refetch: state.refetch,
    ...more,
  });
}

function card(label: string): HTMLElement {
  return screen.getByText(label).closest('div') as HTMLElement;
}

beforeEach(() => {
  state.overview.mockReset();
  state.refetch.mockReset();
});
afterEach(cleanup);

describe('the organization overview', () => {
  it('shows the figures of the period with their change against the period before', () => {
    answer(WEEK);

    render(<OverviewView />);

    expect(state.overview).toHaveBeenLastCalledWith('7d');
    expect(card('Spend')).toHaveTextContent('$121.76');
    expect(card('Spend')).toHaveTextContent('−6% vs previous 7 days');
    expect(card('Requests')).toHaveTextContent('4,464');
    expect(card('Requests')).toHaveTextContent('−3% vs previous 7 days');
    expect(card('Tokens')).toHaveTextContent('18M');
    expect(card('Members with requests')).toHaveTextContent('22/ 24');
    expect(card('Organization limit, this month')).toHaveTextContent('$298.23/ $1,000.00');
  });

  it('draws the two series as charts, and as tables of the same numbers', () => {
    answer(WEEK);

    render(<OverviewView />);

    const members = screen.getByRole('region', { name: 'Members with requests per day' });
    const spend = screen.getByRole('region', { name: 'Spend per day (USD)' });
    expect(within(members).getByTestId('chart')).toHaveAttribute('data-kind', 'line');
    expect(within(members).getByTestId('chart')).toHaveTextContent('Nov 16=16 Nov 17=15');
    expect(within(spend).getByTestId('chart')).toHaveAttribute('data-kind', 'bar');

    fireEvent.click(within(spend).getByRole('button', { name: 'Table' }));

    const rows = within(spend).getAllByRole('row');
    expect(rows.map((row) => row.textContent)).toEqual([
      'Day (UTC)Spend',
      'Nov 16$22.50',
      'Nov 17$25.94',
    ]);
    // The other card keeps its own view.
    expect(within(members).getByTestId('chart')).toBeInTheDocument();
  });

  it('writes an amount short on the axis of the chart, and in full in its tooltip and table', () => {
    answer({ ...WEEK, series: [day('2026-11-16', 1350, 16), day('2026-11-17', 22.5, 15)] });

    render(<OverviewView />);

    const spend = screen.getByRole('region', { name: 'Spend per day (USD)' });
    const chart = within(spend).getByTestId('chart');
    // The axis has room for a few characters: $1,350.00 would be cut at its edge.
    expect(chart).toHaveAttribute('data-ticks', '$1.35K $22.5');
    expect(chart).toHaveAttribute('data-values', '$1,350.00 $22.50');
    // A count is short as it is.
    const members = screen.getByRole('region', { name: 'Members with requests per day' });
    expect(within(members).getByTestId('chart')).toHaveAttribute('data-ticks', '16 15');
  });

  it('lists the models that cost something, the largest first, with the way to all of them', () => {
    answer(WEEK);

    render(<OverviewView />);

    const models = screen.getByRole('region', { name: 'Spend by model' });
    // One grid for the list, each row on its columns: the bars then have one
    // track, whatever the width of each amount.
    const list = within(models).getByRole('list');
    expect(list).toHaveClass('grid');
    for (const row of within(list).getAllByRole('listitem')) {
      expect(row).toHaveClass('grid-cols-subgrid');
    }
    expect(
      within(models)
        .getAllByRole('listitem')
        .map((item) => item.textContent),
    ).toEqual(['glm-4.6$49.92', 'deepseek-v3.2$32.87']);
    expect(within(models).getByRole('link', { name: 'View by model' })).toHaveAttribute(
      'href',
      '/dashboard/organization/usage?view=models',
    );
  });

  it('asks for another period when one is chosen', () => {
    answer(WEEK);

    render(<OverviewView />);
    fireEvent.click(screen.getByRole('button', { name: 'Last 30 days' }));

    expect(state.overview).toHaveBeenLastCalledWith('30d');
    expect(screen.getByRole('button', { name: 'Last 30 days' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('shows a day by its hours', () => {
    answer({
      ...WEEK,
      period: '1d',
      bucket: 'hour',
      series: [
        { ...day('2026-11-17', 1.5, 3), bucket: '2026-11-17T08:00:00Z' },
        { ...day('2026-11-17', 2.5, 5), bucket: '2026-11-17T09:00:00Z' },
      ],
    });

    render(<OverviewView />);

    const members = screen.getByRole('region', { name: 'Members with requests per hour' });
    expect(within(members).getByTestId('chart')).toHaveTextContent('08:00=3 09:00=5');
    expect(screen.getByRole('region', { name: 'Spend per hour (USD)' })).toBeInTheDocument();
  });

  it('says so when the organization has no limit, and compares with nothing when there is no period before', () => {
    answer({ ...WEEK, period: '30d', previous: null, monthly_limit_usd: null });

    render(<OverviewView />);

    expect(card('Organization limit, this month')).toHaveTextContent('$298.23');
    expect(card('Organization limit, this month')).not.toHaveTextContent('/');
    expect(screen.getByText('The organization has no monthly limit.')).toBeInTheDocument();
    expect(screen.queryByText(/vs previous/)).not.toBeInTheDocument();
  });

  it('says a quiet period is quiet instead of drawing an empty chart', () => {
    answer({ ...WEEK, series: [day('2026-11-16', 0, 0), day('2026-11-17', 0, 0)], models: [] });

    render(<OverviewView />);

    expect(screen.queryByTestId('chart')).not.toBeInTheDocument();
    expect(screen.getAllByText('Nothing in this period.')).toHaveLength(3);
  });

  it('reads the native amounts on a ledger that is not in USD', () => {
    answer({
      ...WEEK,
      currency: 'CNY',
      cost: 818.5,
      spent_month: 2004.11,
      monthly_limit: 7000,
      previous: { currency: 'CNY', cost_usd: 129.53, cost: 870.2, requests: 4602 },
    });

    render(<OverviewView />);

    expect(card('Spend')).toHaveTextContent('¥818.50');
    expect(card('Organization limit, this month')).toHaveTextContent('¥2,004.11/ ¥7,000.00');
    // Never the USD equivalents under a yuan sign.
    expect(card('Spend')).not.toHaveTextContent('121.76');
  });

  it('names and writes the spend of each day in the currency the gateway sent with it', () => {
    // The page stayed open while the ledger changed: its own configuration
    // still says USD, and the gateway now answers in yuan.
    answer({
      ...WEEK,
      currency: 'CNY',
      cost: 92.4,
      spent_month: 2004.11,
      monthly_limit: 7000,
      previous: null,
      series: [
        { ...day('2026-11-16', 10, 16), currency: 'CNY', cost: 70 },
        { ...day('2026-11-17', 3.2, 15), currency: 'CNY', cost: 22.4 },
      ],
    });

    render(<OverviewView />);

    // The card of the same answer is in yuan, and so is the figure.
    expect(card('Spend')).toHaveTextContent('¥92.40');
    const spend = screen.getByRole('region', { name: 'Spend per day (CNY)' });
    expect(within(spend).getByTestId('chart')).toHaveAttribute('data-values', '¥70.00 ¥22.40');
    fireEvent.click(within(spend).getByRole('button', { name: 'Table' }));
    expect(
      within(spend)
        .getAllByRole('row')
        .map((row) => row.textContent),
    ).toEqual(['Day (UTC)Spend', 'Nov 16¥70.00', 'Nov 17¥22.40']);
  });

  it('keeps to the currency of the answer on a page that loaded with another one', () => {
    // The other way round: the page loaded on a ledger in yuan, the answer is in USD.
    answer(WEEK);

    render(
      <SiteConfigProvider
        initialConfig={{ ...buildTimeSiteConfig, accounting: { currency: 'CNY' as const } }}
      >
        <OverviewView />
      </SiteConfigProvider>,
    );

    const spend = screen.getByRole('region', { name: 'Spend per day (USD)' });
    expect(within(spend).getByTestId('chart')).toHaveAttribute('data-values', '$22.50 $25.94');
  });

  it('offers to try again when the figures cannot be read', () => {
    answer(undefined, { error: new Error('The gateway is not reachable.') });

    render(<OverviewView />);

    expect(screen.getByRole('alert')).toHaveTextContent('The gateway is not reachable.');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(state.refetch).toHaveBeenCalledTimes(1);
  });

  it('shows that it is loading before the first answer', () => {
    answer(undefined, { isLoading: true });

    render(<OverviewView />);

    expect(screen.getByRole('status')).toHaveTextContent('Loading...');
  });
});
