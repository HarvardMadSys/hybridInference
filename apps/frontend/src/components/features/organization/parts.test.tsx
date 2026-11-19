// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { LimitBar, LimitMeter } from './parts';

afterEach(cleanup);

/** The track of the bar and its filled part. */
function bar(container: HTMLElement): { track: HTMLElement; fill: HTMLElement } {
  const track = container.querySelector('[aria-hidden="true"]') as HTMLElement;
  return { track, fill: track.firstElementChild as HTMLElement };
}

describe('the bar of a limit', () => {
  it.each([
    [0, '0%'],
    // Anything spent shows, even one cent of a large limit.
    [0.01, '3%'],
    [21, '21%'],
    [90, '90%'],
    // Over the limit fills the bar and no more.
    [116, '100%'],
  ])('with %d of 100 spent fills %s of a track of full length', (spent, width) => {
    const { container } = render(<LimitBar spent={spent} limit={100} />);

    const { track, fill } = bar(container);
    expect(track).toHaveClass('w-full');
    expect(fill.style.width).toBe(width);
  });

  it('colors the fill by how near the limit is', () => {
    const fillOf = (spent: number) =>
      bar(render(<LimitBar spent={spent} limit={100} />).container).fill;

    expect(fillOf(50)).toHaveClass('bg-blue-500');
    expect(fillOf(90)).toHaveClass('bg-amber-500');
    expect(fillOf(100)).toHaveClass('bg-red-500');
  });

  it('draws nothing without a limit', () => {
    const { container } = render(<LimitBar spent={12} limit={null} />);

    expect(container).toBeEmptyDOMElement();
  });
});

describe('the month of a member against its limit', () => {
  it('shows the bar and the share of the limit', () => {
    const { container } = render(<LimitMeter spent={76.7} limit={130} limitText="$130.00" />);

    expect(screen.getByText('59% of $130.00')).toBeInTheDocument();
    expect(bar(container).fill.style.width).toBe('59%');
    // The limit is the member's own: nothing more is said.
    expect(screen.queryByText('default')).not.toBeInTheDocument();
  });

  it('says so under the figure when the limit is the default of the organization', () => {
    const own = render(<LimitMeter spent={21} limit={100} limitText="$100.00" />);
    const ownHtml = own.container.innerHTML;
    own.unmount();

    const { container } = render(
      <LimitMeter spent={21} limit={100} limitText="$100.00" isDefault />,
    );

    const word = screen.getByText('default');
    expect(word).toHaveAttribute('title', 'Default limit of the organization');
    // The word hangs below the line and moves nothing: without it, the row is
    // the one a member's own limit has.
    expect(word).toHaveClass('absolute');
    word.remove();
    expect(container.innerHTML).toBe(ownHtml);
  });

  it('says No limit, with no bar, for a member that has none', () => {
    const { container } = render(<LimitMeter spent={12} limit={null} limitText="—" isDefault />);

    expect(screen.getByText('No limit')).toBeInTheDocument();
    expect(container.querySelector('[aria-hidden="true"]')).toBeNull();
    expect(screen.queryByText('default')).not.toBeInTheDocument();
  });
});
