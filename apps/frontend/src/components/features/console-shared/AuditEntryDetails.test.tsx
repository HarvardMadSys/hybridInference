// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import {
  AuditChangeSummary,
  AuditEntryDetails,
  DisclosureChevron,
  fullValue,
  shortValue,
} from './AuditEntryDetails';
import { ModelTokenTable } from './ModelTokenTable';

afterEach(cleanup);

describe('the one-line summary of an audit entry', () => {
  it('says so for an entry without details', () => {
    render(<AuditChangeSummary details={null} />);

    expect(screen.getByText('No details')).toBeInTheDocument();
  });

  it('reads a before and after pair as old, then new', () => {
    const { container } = render(
      <AuditChangeSummary details={{ key: 'monthly_limit', old_value: 50, new_value: 80 }} />,
    );

    expect(container).toHaveTextContent('monthly_limit: 50→ changed to 80');
  });

  it('lists the fields that an update changed', () => {
    const { container } = render(
      <AuditChangeSummary details={{ updated_fields: ['name', 'role'] }} />,
    );

    expect(container).toHaveTextContent('Changed name, role');
  });

  it('shows the first two set fields of anything else, and how many more there are', () => {
    const { container } = render(
      <AuditChangeSummary details={{ empty: '', a: 1, b: 'two', c: [3], d: null }} />,
    );

    expect(container).toHaveTextContent('a 1·b two+3 more');
  });
});

describe('an opened audit entry', () => {
  const details = { old: ['m1'], new: ['m1', 'm2'], note: 'by hand' };

  it('shows the lines it was given, then each field in full, then the stored JSON', () => {
    const { container } = render(
      <AuditEntryDetails
        meta={[
          ['Action', 'organization.member_models.set', 'font-mono'],
          ['Actor', 'ada@example.edu'],
        ]}
        details={details}
      />,
    );

    expect(screen.getByText('Action').nextElementSibling).toHaveClass('font-mono');
    expect(screen.getByText('ada@example.edu')).toBeInTheDocument();
    // An array is written as indented JSON, one element on a line.
    expect(screen.getByText('note').nextElementSibling).toHaveTextContent('by hand');
    expect(screen.getByText('new').nextElementSibling?.textContent).toBe(
      JSON.stringify(['m1', 'm2'], null, 2),
    );
    expect(screen.getByText('Raw JSON')).toBeInTheDocument();
    expect(container.querySelector('pre')?.textContent).toBe(JSON.stringify(details, null, 2));
  });

  it('leaves the stored JSON out where the page does not offer it, or has none', () => {
    const { rerender } = render(<AuditEntryDetails meta={[]} details={details} raw={false} />);
    expect(screen.queryByText('Raw JSON')).not.toBeInTheDocument();
    expect(screen.getByText('note')).toBeInTheDocument();

    rerender(<AuditEntryDetails meta={[['Actor', 'System']]} details={null} />);
    expect(screen.queryByText('Raw JSON')).not.toBeInTheDocument();
    expect(screen.getByText('System')).toBeInTheDocument();
  });

  it('turns its mark when the row is open', () => {
    const { container, rerender } = render(<DisclosureChevron open={false} />);
    expect(container.querySelector('svg')).not.toHaveClass('rotate-90');

    rerender(<DisclosureChevron open />);
    expect(container.querySelector('svg')).toHaveClass('rotate-90');
  });
});

describe('a value of an audit entry', () => {
  it('is cut short for a row, and whole in the details', () => {
    expect(shortValue('x'.repeat(60))).toBe(`${'x'.repeat(48)}…`);
    expect(shortValue({ a: 1 })).toBe('{"a":1}');
    expect(shortValue(null)).toBe('—');
    expect(fullValue('x'.repeat(60))).toBe('x'.repeat(60));
    expect(fullValue(false)).toBe('false');
    expect(fullValue(undefined)).toBe('—');
  });
});

describe('the token table of the models', () => {
  const rows = [
    {
      model_id: 'model-a',
      input_tokens: 1200,
      output_tokens: 300,
      cached_tokens: 50,
      reasoning_tokens: 7,
      request_count: 4,
      cost: 'USD 0.12',
    },
  ];
  const count = (value: number) => `n${value}`;
  const headers = () => screen.getAllByRole('columnheader').map((cell) => cell.textContent);

  it('has every column under its title, in the order of the table', () => {
    render(<ModelTokenTable title="Provider A" rows={rows} formatCount={count} />);

    expect(screen.getByText('Provider A')).toBeInTheDocument();
    expect(headers()).toEqual([
      'Model',
      'Input',
      'Output',
      'Cached',
      'Reasoning',
      'Requests',
      'Cost',
    ]);
    expect(screen.getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      'model-a',
      'n1200',
      'n300',
      'n50',
      'n7',
      'n4',
      'USD 0.12',
    ]);
  });

  it('shows only the columns the page chose, and no heading without a title', () => {
    render(
      <ModelTokenTable
        rows={rows}
        formatCount={count}
        columns={['requests', 'input', 'output', 'cached']}
      />,
    );

    // The order is the table's, not the order the page named them in.
    expect(headers()).toEqual(['Model', 'Input', 'Output', 'Cached', 'Requests']);
    expect(screen.queryByText('Provider A')).not.toBeInTheDocument();
    // The last column that is shown has the padding of the edge.
    expect(screen.getByRole('columnheader', { name: 'Requests' })).toHaveClass('px-4');
    expect(screen.getByRole('columnheader', { name: 'Cached' })).toHaveClass('px-3');
  });
});
