// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Pagination } from './Pagination';

afterEach(cleanup);

describe('Pagination', () => {
  it('shows the current row range and total', () => {
    render(
      <Pagination
        page={0}
        pageSize={100}
        total={250}
        count={100}
        onPageChange={vi.fn()}
        onPageSizeChange={vi.fn()}
      />,
    );
    expect(screen.getByText(/Showing/)).toHaveTextContent('Showing 1–100 of 250');
    expect(screen.getByText(/Page/)).toHaveTextContent('Page 1 of 3');
  });

  it('disables Previous on the first page and enables Next', () => {
    render(
      <Pagination
        page={0}
        pageSize={50}
        total={120}
        count={50}
        onPageChange={vi.fn()}
        onPageSizeChange={vi.fn()}
      />,
    );
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled();
  });

  it('disables Next on the last page', () => {
    render(
      <Pagination
        page={2}
        pageSize={50}
        total={120}
        count={20}
        onPageChange={vi.fn()}
        onPageSizeChange={vi.fn()}
      />,
    );
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
    expect(screen.getByText(/Showing/)).toHaveTextContent('Showing 101–120 of 120');
  });

  it('advances the page when Next is clicked', () => {
    const onPageChange = vi.fn();
    render(
      <Pagination
        page={0}
        pageSize={50}
        total={120}
        count={50}
        onPageChange={onPageChange}
        onPageSizeChange={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(onPageChange).toHaveBeenCalledWith(1);
  });

  it('reports a new page size selection', () => {
    const onPageSizeChange = vi.fn();
    render(
      <Pagination
        page={0}
        pageSize={50}
        total={120}
        count={50}
        onPageChange={vi.fn()}
        onPageSizeChange={onPageSizeChange}
      />,
    );
    fireEvent.change(screen.getByLabelText('Rows per page'), { target: { value: '250' } });
    expect(onPageSizeChange).toHaveBeenCalledWith(250);
  });

  it('renders an empty state with a single page when there are no users', () => {
    render(
      <Pagination
        page={0}
        pageSize={50}
        total={0}
        count={0}
        onPageChange={vi.fn()}
        onPageSizeChange={vi.fn()}
      />,
    );
    expect(screen.getByText('No users')).toBeInTheDocument();
    expect(screen.getByText(/Page/)).toHaveTextContent('Page 1 of 1');
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  });
});
