// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, renderHook, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { LimitBar, LimitMeter, useListPosition } from './parts';

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
    // Near from 80%, as the filter of the member list counts it.
    expect(fillOf(79)).toHaveClass('bg-blue-500');
    expect(fillOf(80)).toHaveClass('bg-amber-500');
    // On the ratio itself, as the gateway compares it, not on a rounded figure:
    // 79.6% is not yet near, and 99.6% is not yet over.
    expect(fillOf(79.6)).toHaveClass('bg-blue-500');
    expect(fillOf(99.6)).toHaveClass('bg-amber-500');
    expect(fillOf(100)).toHaveClass('bg-red-500');
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

  it.each([
    // Not yet near: the list filter and the card do not count this member.
    [79.6, '79% of $100.00', 'text-gray-500'],
    [80, '80% of $100.00', 'text-amber-600'],
    // Not yet over: the month is still under the limit.
    [99.6, '99% of $100.00', 'text-amber-600'],
    [100, '100% of $100.00', 'text-red-600'],
    [116.4, '116% of $100.00', 'text-red-600'],
  ])('never claims a mark the member has not reached: %d of 100', (spent, text, tone) => {
    render(<LimitMeter spent={spent} limit={100} limitText="$100.00" />);

    expect(screen.getByText(text)).toHaveClass(tone);
  });

  it('says so under the figure when the limit is the default of the organization', () => {
    const own = render(<LimitMeter spent={21} limit={100} limitText="$100.00" />);
    const ownHtml = own.container.innerHTML;
    own.unmount();

    const { container } = render(
      <LimitMeter spent={21} limit={100} limitText="$100.00" source="default" />,
    );

    const word = screen.getByText('default');
    expect(word).toHaveAttribute('title', 'Default limit of the organization');
    // The word hangs below the line and moves nothing: without it, the row is
    // the one a member's own limit has.
    expect(word).toHaveClass('absolute');
    word.remove();
    expect(container.innerHTML).toBe(ownHtml);
  });

  it('says so in the same place when the limit is the platform administrator’s', () => {
    render(<LimitMeter spent={9} limit={10} limitText="$10.00" source="platform" />);

    const word = screen.getByText('platform');
    expect(word).toHaveAttribute('title', "Platform administrator's limit");
    expect(word).toHaveClass('absolute');
    expect(screen.queryByText('default')).not.toBeInTheDocument();
  });

  it('says No limit, with no bar, for a member that has none', () => {
    const { container } = render(
      <LimitMeter spent={12} limit={null} limitText="—" source="default" />,
    );

    expect(screen.getByText('No limit')).toBeInTheDocument();
    expect(container.querySelector('[aria-hidden="true"]')).toBeNull();
    expect(screen.queryByText('default')).not.toBeInTheDocument();
  });
});

describe('the position in a paged list', () => {
  /** The hook as a list uses it, with each render it had: its filters and its position. */
  function list() {
    const renders: Array<[string, number]> = [];
    const hook = renderHook(
      ({ filters }: { filters: string }) => {
        const [position, setPosition] = useListPosition(filters, 0);
        renders.push([filters, position]);
        return { position, setPosition };
      },
      { initialProps: { filters: '7d' } },
    );
    return { ...hook, renders };
  }

  it('is kept while the filters stay as they are', () => {
    const { result, rerender } = list();

    act(() => result.current.setPosition(2));
    rerender({ filters: '7d' });

    expect(result.current.position).toBe(2);
  });

  it('starts again with other filters, and does not come back with the earlier ones', () => {
    const { result, rerender } = list();
    act(() => result.current.setPosition(2));

    rerender({ filters: '30d' });
    expect(result.current.position).toBe(0);

    rerender({ filters: '7d' });
    expect(result.current.position).toBe(0);
  });

  it('never gives the old position with other filters, not for one render', () => {
    // One such render would ask the gateway for a page nobody asked for.
    const { result, rerender, renders } = list();
    act(() => result.current.setPosition(2));

    rerender({ filters: '30d' });

    expect(renders).toContainEqual(['30d', 0]);
    expect(renders).not.toContainEqual(['30d', 2]);
  });

  it('moves within the other filters from their first position', () => {
    const { result, rerender } = list();
    act(() => result.current.setPosition(2));
    rerender({ filters: '30d' });

    act(() => result.current.setPosition(1));

    expect(result.current.position).toBe(1);
  });
});
