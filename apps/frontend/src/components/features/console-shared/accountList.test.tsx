// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SearchBox } from './SearchBox';
import { SpendCells, type SpendColumn } from './SpendCells';
import { StatusPill } from './StatusPill';
import { SummaryCard } from './SummaryCard';
import { ThresholdInput } from './ThresholdInput';

// jsdom cannot lay out recharts; the cell that holds the line is what is tested.
vi.mock('./Sparkline', () => ({
  Sparkline: ({ values, label }: { values: unknown[]; label: string }) => (
    <span data-testid="trend" aria-label={label}>
      {values.length}
    </span>
  ),
}));

afterEach(cleanup);

function row(ui: ReactNode) {
  return render(
    <table>
      <tbody>
        <tr>{ui}</tr>
      </tbody>
    </table>,
  );
}

const FIGURES = {
  today: { amount: 1.5, text: 'USD 1.50' },
  trend: [0, 1, 2, 0, 0, 3, 1.5],
  trendLabel: '7-day cost trend',
  month: { amount: 0, text: 'USD 0.00' },
  alltime: { amount: 40, text: 'USD 40.00' },
};

describe('the spend cells of a row', () => {
  it('draws today, the trend, the month and all time, and dims a zero', () => {
    row(<SpendCells {...FIGURES} />);

    const cells = screen.getAllByRole('cell');
    expect(cells.map((cell) => cell.textContent)).toEqual([
      'USD 1.50',
      '7',
      'USD 0.00',
      'USD 40.00',
    ]);
    expect(cells[0]).toHaveClass('text-gray-900');
    expect(cells[2]).toHaveClass('text-gray-300');
    expect(cells[3]).toHaveClass('text-gray-600');
    expect(screen.getByTestId('trend')).toHaveAccessibleName('7-day cost trend');
  });

  it('leaves out a column the list does not show', () => {
    const shown = new Set<SpendColumn>(['today', 'alltime']);
    row(<SpendCells {...FIGURES} shows={(column) => shown.has(column)} />);

    expect(screen.getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      'USD 1.50',
      'USD 40.00',
    ]);
  });

  it("adds the list's own classes to a cell", () => {
    row(
      <SpendCells
        {...FIGURES}
        cellClass={(column) => (column === 'today' ? 'bg-amber-100' : 'border-l')}
        numberClass="px-4 text-right"
      />,
    );

    const cells = screen.getAllByRole('cell');
    expect(cells[0]).toHaveClass('bg-amber-100', 'px-4');
    expect(cells[1]).toHaveClass('border-l');
    expect(cells[2]).not.toHaveClass('bg-amber-100');
  });
});

describe('the summary card of a list', () => {
  const items = ['a', 'b', 'c', 'd'].map((id) => ({
    id,
    label: `${id}@example.edu`,
    value: `USD ${id}`,
  }));

  it('shows the count and the first three accounts, and opens its view', () => {
    const onClick = vi.fn();
    render(
      <SummaryCard
        title="Top spenders today"
        accent="emerald"
        count={4}
        items={items}
        empty="No members right now"
        onClick={onClick}
      />,
    );

    const card = screen.getByRole('button', { name: /Top spenders today/ });
    expect(card).toHaveTextContent('4');
    expect(screen.getAllByRole('listitem')).toHaveLength(3);
    expect(screen.queryByText('d@example.edu')).not.toBeInTheDocument();
    fireEvent.click(card);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('shows as many accounts as the page asks for', () => {
    render(
      <SummaryCard
        title="Top spenders today"
        accent="emerald"
        count={4}
        items={items}
        empty="No members right now"
        shown={5}
        onClick={() => {}}
      />,
    );

    expect(screen.getAllByRole('listitem')).toHaveLength(4);
  });

  it('says so when no account is in the view, and marks the view that is open', () => {
    render(
      <SummaryCard
        title="Near the limit"
        accent="amber"
        count={0}
        items={[]}
        empty="No members right now"
        active
        onClick={() => {}}
      />,
    );

    expect(screen.getByText('No members right now')).toBeInTheDocument();
    expect(screen.getByRole('button')).toHaveAttribute('aria-pressed', 'true');
  });

  it('shows a dash for the count while the figures are on their way', () => {
    render(
      <SummaryCard
        title="Near the limit"
        accent="amber"
        count={0}
        items={[]}
        empty="No members right now"
        loading
        onClick={() => {}}
      />,
    );

    expect(screen.getByRole('button')).toHaveTextContent('—');
    expect(screen.queryByText('No members right now')).not.toBeInTheDocument();
  });
});

describe('the threshold filter', () => {
  it('gives a number as it is typed, and says nothing new for an empty field', () => {
    const onChange = vi.fn();
    render(<ThresholdInput label="Today at least" value={null} onChange={onChange} step="0.01" />);

    const input = screen.getByLabelText('Today at least');
    fireEvent.change(input, { target: { value: '2.5' } });
    expect(onChange).toHaveBeenLastCalledWith(2.5);
    fireEvent.change(input, { target: { value: '' } });
    // The value the caller holds is still null, so there is no second call.
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it('takes a value set from outside, and its lowest value', () => {
    const { rerender } = render(
      <ThresholdInput label="Active in last (h)" value={24} onChange={() => {}} min={1} />,
    );
    const input = screen.getByLabelText('Active in last (h)');
    expect(input).toHaveValue(24);
    expect(input).toHaveAttribute('min', '1');

    rerender(
      <ThresholdInput label="Active in last (h)" value={null} onChange={() => {}} min={1} />,
    );
    expect(input).toHaveValue(null);
  });
});

describe('the search box and the status pill', () => {
  it('passes each keystroke on, under the name it was given', () => {
    const onChange = vi.fn();
    render(<SearchBox value="" onChange={onChange} placeholder="Search" label="Search members" />);

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search members' }), {
      target: { value: 'ada' },
    });
    expect(onChange).toHaveBeenCalledWith('ada');
  });

  it('draws the word of a status in its colors', () => {
    render(<StatusPill tone={{ label: 'Active', pill: 'bg-emerald-50', dot: 'bg-emerald-500' }} />);

    expect(screen.getByText('Active')).toHaveClass('bg-emerald-50', 'whitespace-nowrap');
  });
});
