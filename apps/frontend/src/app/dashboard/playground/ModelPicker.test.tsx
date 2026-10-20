// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ModelPicker, type PlaygroundModel } from './ModelPicker';

const models: PlaygroundModel[] = [
  {
    id: 'kimi-code-standard',
    name: 'Kimi K2.7 Code (only support coding agents)',
    provider: 'moonshot',
    providers: [{ id: 'moonshot-direct', name: 'Moonshot API' }],
  },
  {
    id: 'kimi-code-fast',
    name: 'Kimi K2.7 Code (only support coding agents)',
    provider: 'chutes',
    providers: [{ id: 'chutes-fast', name: 'Chutes' }],
  },
  {
    id: 'glm-5.1',
    name: 'GLM 5.1',
    provider: 'zai',
    providers: [{ id: 'zai-direct', name: 'Z.AI API' }],
  },
];

function openPicker() {
  const trigger = screen.getByRole('button', { name: /^Model:/ });
  fireEvent.click(trigger);
  return { trigger, search: screen.getByRole('combobox', { name: 'Search models' }) };
}

describe('ModelPicker', () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(cleanup);

  it.each([
    ['name', 'GLM 5.1', 'glm-5.1'],
    ['model ID', 'KIMI-CODE-FAST', 'kimi-code-fast'],
    ['provider', 'moonshot', 'kimi-code-standard'],
    ['provider name', 'Z.AI API', 'glm-5.1'],
    ['endpoint ID', 'chutes-fast', 'kimi-code-fast'],
  ])('searches by %s and selects the matching model ID', (_field, query, expectedId) => {
    const onChange = vi.fn();
    render(<ModelPicker models={models} value="" onChange={onChange} />);
    const { trigger, search } = openPicker();

    expect(search).toHaveFocus();
    fireEvent.change(search, { target: { value: query } });
    const options = screen.getAllByRole('option');
    expect(options).toHaveLength(1);
    expect(options[0]).toHaveAccessibleName(new RegExp(expectedId));
    fireEvent.click(options[0]);

    expect(onChange).toHaveBeenCalledExactlyOnceWith(expectedId);
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it('distinguishes identical display names by model ID', () => {
    const onChange = vi.fn();
    render(<ModelPicker models={models} value={models[0].id} onChange={onChange} />);
    const { search } = openPicker();
    fireEvent.change(search, { target: { value: 'Kimi K2.7' } });

    expect(screen.getAllByRole('option')).toHaveLength(2);
    expect(screen.getByRole('option', { name: /kimi-code-standard/ })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    fireEvent.click(screen.getByRole('option', { name: /kimi-code-fast/ }));
    expect(onChange).toHaveBeenCalledExactlyOnceWith('kimi-code-fast');
  });

  it('shows an empty search without selecting a stale result', () => {
    const onChange = vi.fn();
    render(<ModelPicker models={models} value={models[0].id} onChange={onChange} />);
    const { search } = openPicker();
    fireEvent.change(search, { target: { value: 'no-such-model' } });

    expect(screen.getByText('No models found')).toBeInTheDocument();
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
    fireEvent.keyDown(search, { key: 'ArrowDown' });
    fireEvent.keyDown(search, { key: 'Enter' });
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.change(search, { target: { value: '' } });
    expect(screen.getAllByRole('option')).toHaveLength(models.length);
  });

  it('selects with arrow keys and Enter, and restores focus on Escape', () => {
    const onChange = vi.fn();
    render(<ModelPicker models={models} value={models[0].id} onChange={onChange} />);
    const { trigger, search } = openPicker();
    fireEvent.keyDown(search, { key: 'ArrowDown' });
    expect(search).toHaveAttribute(
      'aria-activedescendant',
      screen.getByRole('option', { name: /kimi-code-fast/ }).id,
    );
    fireEvent.keyDown(search, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledExactlyOnceWith('kimi-code-fast');
    expect(trigger).toHaveFocus();

    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByRole('combobox', { name: 'Search models' }), {
      key: 'Escape',
    });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it('dismisses on outside interaction and lets Tab leave without selecting', () => {
    const onChange = vi.fn();
    render(
      <>
        <ModelPicker models={models} value={models[0].id} onChange={onChange} />
        <button type="button">Next setting</button>
      </>,
    );
    openPicker();
    const outside = screen.getByRole('button', { name: 'Next setting' });
    fireEvent.pointerDown(outside);
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

    const { search } = openPicker();
    const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
    fireEvent(search, tab);
    expect(tab.defaultPrevented).toBe(false);
    // jsdom does not implement the browser's default Tab focus movement.
    act(() => outside.focus());
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(outside).toHaveFocus();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('closes and disables an open picker when streaming starts', () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <ModelPicker models={models} value={models[0].id} onChange={onChange} />,
    );
    const { trigger } = openPicker();
    rerender(<ModelPicker models={models} value={models[0].id} onChange={onChange} disabled />);

    expect(trigger).toBeDisabled();
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    fireEvent.click(trigger);
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });
});
