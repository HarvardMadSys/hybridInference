// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AuthAppearanceProvider, NEUTRAL_AUTH_APPEARANCE } from '@/site-ui/appearance';
import { AuthMethodSelector } from './AuthMethodSelector';

afterEach(cleanup);

describe.each([
  ['neutral', NEUTRAL_AUTH_APPEARANCE],
  ['module', { ...NEUTRAL_AUTH_APPEARANCE, linkButton: 'module-link' }],
] as const)('%s authentication appearance', (_name, appearance) => {
  it('names login methods independently of signup and preserves selection and style hooks', () => {
    const onChange = vi.fn();
    const view = (value: 'email' | 'phone') => (
      <AuthAppearanceProvider value={appearance}>
        <AuthMethodSelector purpose="login" value={value} onChange={onChange} />
      </AuthAppearanceProvider>
    );
    const { rerender } = render(view('email'));
    const group = screen.getByRole('group', { name: 'Login method' });
    expect(group).toHaveAttribute('data-auth', 'secondary-actions');
    expect(group).toHaveAttribute('data-auth-method-selector');
    expect(screen.getByRole('button', { name: 'Password login' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    fireEvent.click(screen.getByRole('button', { name: 'SMS code login' }));
    expect(onChange).toHaveBeenCalledWith('phone');
    rerender(view('phone'));
    expect(screen.getByRole('button', { name: 'SMS code login' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(screen.getByRole('button', { name: 'Password login' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });

  it('keeps the default signup choices and exposes the same styling hook', () => {
    render(
      <AuthAppearanceProvider value={appearance}>
        <AuthMethodSelector value="email" onChange={vi.fn()} />
      </AuthAppearanceProvider>,
    );
    expect(screen.getByRole('group', { name: 'Account method' })).toHaveAttribute(
      'data-auth-method-selector',
    );
    expect(screen.getByRole('button', { name: 'Email and password' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Phone verification code' })).toBeInTheDocument();
  });
});
