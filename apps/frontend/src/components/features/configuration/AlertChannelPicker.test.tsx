// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AlertChannelPicker } from './AlertChannelPicker';
import type { AlertChannelChoice } from './alertNotificationDrafts';

afterEach(cleanup);

function Harness({
  disabled = false,
  onChange = vi.fn(),
  onSubmit = vi.fn(),
}: {
  disabled?: boolean;
  onChange?: (channel: AlertChannelChoice) => void;
  onSubmit?: () => void;
}) {
  const [value, setValue] = useState<AlertChannelChoice>('slack');
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <label htmlFor="channel">Notification channel</label>
      <AlertChannelPicker
        id="channel"
        value={value}
        disabled={disabled}
        onChange={(channel) => {
          setValue(channel);
          onChange(channel);
        }}
      />
      <input aria-label="Next field" />
    </form>
  );
}

function focus(element: HTMLElement) {
  act(() => element.focus());
}

function trigger() {
  return screen.getByRole('combobox', { name: 'Notification channel' });
}

function expectHighlighted(label: string) {
  expect(trigger()).toHaveAttribute(
    'aria-activedescendant',
    screen.getByRole('option', { name: label }).id,
  );
}

describe('AlertChannelPicker', () => {
  it('supports arrow, Home, End, Enter and Space navigation without submitting the form', () => {
    const onChange = vi.fn();
    const onSubmit = vi.fn();
    render(<Harness onChange={onChange} onSubmit={onSubmit} />);
    focus(trigger());

    fireEvent.keyDown(trigger(), { key: 'ArrowDown' });
    expectHighlighted('Slack');
    fireEvent.keyDown(trigger(), { key: 'ArrowDown' });
    expectHighlighted('WeCom');
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.keyDown(trigger(), { key: 'Enter' });
    expect(onChange).toHaveBeenLastCalledWith('wecom');
    expect(trigger()).toHaveTextContent('WeCom');
    expect(trigger()).toHaveFocus();
    expect(trigger()).toHaveAttribute('aria-expanded', 'false');

    fireEvent.keyDown(trigger(), { key: ' ' });
    fireEvent.keyDown(trigger(), { key: 'Home' });
    expectHighlighted('None');
    fireEvent.keyDown(trigger(), { key: 'End' });
    expectHighlighted('WeCom');
    fireEvent.keyDown(trigger(), { key: 'ArrowUp' });
    expectHighlighted('Slack');
    fireEvent.keyDown(trigger(), { key: ' ' });
    expect(onChange).toHaveBeenLastCalledWith('slack');
    expect(onSubmit).not.toHaveBeenCalled();
    expect(trigger()).toHaveFocus();
  });

  it('dismisses Escape without changing selection or losing focus', () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    focus(trigger());
    fireEvent.keyDown(trigger(), { key: 'End' });
    expectHighlighted('WeCom');
    fireEvent.keyDown(trigger(), { key: 'Escape' });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
    expect(trigger()).toHaveTextContent('Slack');
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(trigger());
    expectHighlighted('Slack');
  });

  it('lets Tab leave the control without committing the highlighted option', () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    focus(trigger());
    fireEvent.keyDown(trigger(), { key: 'End' });
    // A true return means the default Tab action was not prevented.
    expect(fireEvent.keyDown(trigger(), { key: 'Tab' })).toBe(true);
    focus(screen.getByLabelText('Next field'));
    expect(screen.getByLabelText('Next field')).toHaveFocus();
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('dismisses outside pointer or focus without stealing focus', () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    fireEvent.click(trigger());
    fireEvent.pointerDown(screen.getByLabelText('Next field'));
    focus(screen.getByLabelText('Next field'));
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Next field')).toHaveFocus();

    focus(trigger());
    fireEvent.click(trigger());
    focus(screen.getByLabelText('Next field'));
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Next field')).toHaveFocus();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('closes when disabled and cannot reopen or select until re-enabled', () => {
    const onChange = vi.fn();
    const { rerender } = render(<Harness onChange={onChange} />);
    fireEvent.click(trigger());
    expect(screen.getByRole('listbox')).toBeInTheDocument();
    rerender(<Harness onChange={onChange} disabled />);
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(trigger()).toBeDisabled();
    fireEvent.click(trigger());
    fireEvent.keyDown(trigger(), { key: 'End' });
    fireEvent.keyDown(trigger(), { key: 'Enter' });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
    rerender(<Harness onChange={onChange} />);
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    fireEvent.click(trigger());
    expect(screen.getByRole('listbox')).toBeInTheDocument();
  });

  it('uses English-only labels and selecting the current option preserves its draft', () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    fireEvent.click(trigger());
    expect(
      screen.getAllByRole('option').map((option) => option.getAttribute('aria-label')),
    ).toEqual(['None', 'Slack', 'WeCom']);
    expect(screen.queryByText(/企业微信/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('option', { name: 'Slack' }));
    expect(onChange).not.toHaveBeenCalled();
    expect(trigger()).toHaveFocus();
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });
});
