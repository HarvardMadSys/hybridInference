// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemberCard, type MemberCardRow } from './MemberCards';

afterEach(cleanup);

const ROWS: MemberCardRow[] = [
  { id: 'u-min', name: 'Min Li', value: '116%', share: 1.16, tone: 'over' },
  { id: 'u-lei', name: 'Lei Wang', value: '90%', share: 0.9, tone: 'near' },
  { id: 'u-bo', name: 'Bo Huang', value: '$0.00', share: 0 },
];

function card(props: Partial<Parameters<typeof MemberCard>[0]> = {}) {
  const onToggle = vi.fn();
  render(
    <MemberCard
      title="Near or over the limit"
      count="2 members"
      rows={ROWS}
      empty="No members right now"
      active={false}
      showLabel="Show in the list"
      clearLabel="Shown in the list · Clear"
      onToggle={onToggle}
      {...props}
    />,
  );
  return { button: screen.getByRole('button'), onToggle };
}

/** The filled part of each row's bar: its width and its color. */
function bars(button: HTMLElement) {
  return [...button.querySelectorAll<HTMLElement>('.overflow-hidden > span')].map((fill) => [
    fill.style.width,
    fill.className.match(/bg-(blue|amber|red)-500/)?.[1],
  ]);
}

describe('a card above the member list', () => {
  it('names the members with their figures, and says in words how many it holds', () => {
    const { button } = card();

    expect(button).toHaveTextContent('Near or over the limit');
    expect(button).toHaveTextContent('2 members');
    expect(button).toHaveTextContent('Min Li116%');
    expect(button).toHaveTextContent('Lei Wang90%');
  });

  it('draws each figure as a bar: full at most, visible at least, in the color of its tone', () => {
    const { button } = card();

    expect(bars(button)).toEqual([
      ['100%', 'red'],
      ['90%', 'amber'],
      // Nothing draws no fill, as on the bar of a limit; the color is that of a row without a tone.
      ['0%', 'blue'],
    ]);
  });

  it('draws enough to be seen for a share that is almost nothing', () => {
    const { button } = card({
      rows: [{ id: 'u-small', name: 'Shan Luo', value: '$0.01', share: 0.004 }],
    });

    expect(bars(button)).toEqual([['3%', 'blue']]);
  });

  it('keeps one name for a screen reader: the line at the foot is for the eye', () => {
    const { button } = card({ active: true });

    const foot = [...button.querySelectorAll('span')].find((span) =>
      span.textContent?.startsWith('Shown in the list'),
    );
    expect(foot).toHaveAttribute('aria-hidden', 'true');
    // The state of the switch is the button's own.
    expect(screen.getByRole('button', { pressed: true })).toBe(button);
    expect(button).toHaveAccessibleName(/^Near or over the limit/);
    expect(button).not.toHaveAccessibleName(/Clear/);
  });

  it('is a switch: off it says what a press shows, on it says that a press clears', () => {
    const { button, onToggle } = card();
    expect(button).toHaveAttribute('aria-pressed', 'false');
    expect(button).toHaveTextContent('Show in the list');
    expect(button).not.toHaveTextContent('Clear');

    fireEvent.click(button);
    expect(onToggle).toHaveBeenCalledTimes(1);
    cleanup();

    const on = card({ active: true });
    expect(on.button).toHaveAttribute('aria-pressed', 'true');
    expect(on.button).toHaveTextContent('Shown in the list · Clear');
    expect(on.button).toHaveClass('ring-2');
  });

  it('says that it has no member, and shows no count then', () => {
    const { button } = card({ rows: [], count: undefined });

    expect(button).toHaveTextContent('No members right now');
    expect(button).not.toHaveTextContent('2 members');
  });

  it('shows a dash while its members are read, never the words for none', () => {
    const { button } = card({ rows: [], count: undefined, loading: true });

    expect(button).toHaveTextContent('—');
    expect(button).not.toHaveTextContent('No members right now');
  });
});
