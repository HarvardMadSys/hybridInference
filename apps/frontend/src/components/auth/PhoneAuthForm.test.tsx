// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/script', () => ({ default: () => null }));
const wording = vi.hoisted(() => ({ messages: {} as Record<string, string> }));
vi.mock('@/components/providers/useT', () => ({
  useT: () => (slot: string, fallback: string) => wording.messages[slot] ?? fallback,
}));
vi.mock('@/lib/api/auth', () => ({
  sendPhoneCode: vi.fn(),
  bindPhone: vi.fn(),
  signupPhone: vi.fn(),
}));

import { sendPhoneCode, bindPhone, signupPhone } from '@/lib/api/auth';
import { APIError } from '@/lib/utils/errors';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import { PhoneAuthForm } from './PhoneAuthForm';

function enterPhone(phone = '13812345678') {
  fireEvent.change(screen.getByLabelText('Mobile number'), { target: { value: phone } });
}

beforeEach(() => {
  vi.clearAllMocks();
  wording.messages = {};
});
afterEach(() => {
  cleanup();
  delete window.turnstile;
});

describe('phone verification', () => {
  it('translates the known code-sent response through the distribution messages', async () => {
    wording.messages['auth.phone.code_sent'] = '如果手机号符合条件，验证码已发送。';
    vi.mocked(sendPhoneCode).mockResolvedValueOnce({
      message: 'If this phone number is eligible, a verification code has been sent.',
      retry_after: 60,
    });
    render(<PhoneAuthForm purpose="login" onLogin={vi.fn()} />);
    enterPhone();
    fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));
    expect(await screen.findByText('如果手机号符合条件，验证码已发送。')).toBeInTheDocument();
  });

  it('translates known binding success and preserves admin-authored suspension details', async () => {
    wording.messages['auth.phone.bind_success'] = '手机号绑定成功。';
    vi.mocked(bindPhone).mockResolvedValueOnce({ message: 'Phone number bound successfully.' });
    const first = render(
      <PhoneAuthForm purpose="bind" onSuccess={vi.fn().mockResolvedValue(undefined)} />,
    );
    enterPhone();
    fireEvent.change(screen.getByLabelText('Verification code'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Bind mobile number' }));
    expect(await screen.findByText('手机号绑定成功。')).toBeInTheDocument();
    first.unmount();
    render(
      <PhoneAuthForm
        purpose="login"
        onLogin={vi.fn().mockRejectedValue(
          new APIError('ACCOUNT_SUSPENDED', 'Suspended', 403, {
            suspension_message: 'Contact your project administrator.',
          }),
        )}
      />,
    );
    enterPhone();
    fireEvent.change(screen.getByLabelText('Verification code'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Log In' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Contact your project administrator.',
    );
  });

  it.each([
    [
      false,
      'Account created successfully. You can now log in.',
      'auth.phone.signup_success',
      '账户创建成功，可以登录。',
    ],
    [
      true,
      'Account created successfully. Your registration is pending admin approval.',
      'auth.phone.signup_pending',
      '注册已提交，等待管理员审核。',
    ],
  ])(
    'translates the known phone signup result when requires_approval is %s',
    async (requiresApproval, message, slot, translated) => {
      wording.messages[slot] = translated;
      const onSuccess = vi.fn();
      vi.mocked(signupPhone).mockResolvedValueOnce({
        email: null,
        phone: '+8613812345678',
        user_id: 'phone-user',
        requires_approval: requiresApproval,
        message,
      });
      render(<PhoneAuthForm purpose="signup" onSuccess={onSuccess} />);
      enterPhone();
      fireEvent.change(screen.getByLabelText('Verification code'), { target: { value: '123456' } });
      fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'Phone User' } });
      fireEvent.click(screen.getByRole('button', { name: 'Sign Up' }));
      await waitFor(() =>
        expect(onSuccess).toHaveBeenCalledWith(
          expect.objectContaining({ message: translated, requires_approval: requiresApproval }),
        ),
      );
    },
  );
  it('validates phone syntax before sending and respects the server resend delay', async () => {
    vi.mocked(sendPhoneCode).mockResolvedValueOnce({ message: 'Code sent.', retry_after: 90 });
    render(<PhoneAuthForm purpose="login" onLogin={vi.fn()} />);
    enterPhone('+0123');
    fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));
    expect(
      await screen.findByText('Enter a phone number with its country code.'),
    ).toBeInTheDocument();
    expect(sendPhoneCode).not.toHaveBeenCalled();
    enterPhone('+8613812345678');
    fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));
    fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));
    expect(await screen.findByText('Code sent.')).toBeInTheDocument();
    expect(sendPhoneCode).toHaveBeenCalledWith({
      phone: '+8613812345678',
      purpose: 'login',
      turnstileToken: undefined,
    });
    expect(sendPhoneCode).toHaveBeenCalledOnce();
    expect(screen.getByRole('button', { name: 'Send again in 90s' })).toBeDisabled();
  });

  it('clears a code when the phone changes and refuses a malformed code', async () => {
    const onLogin = vi.fn();
    render(<PhoneAuthForm purpose="login" onLogin={onLogin} />);
    enterPhone();
    fireEvent.change(screen.getByLabelText('Verification code'), { target: { value: '123456' } });
    enterPhone('13912345678');
    expect(screen.getByLabelText('Verification code')).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: 'Log In' }));
    expect(await screen.findByText('Enter the six-digit verification code.')).toBeInTheDocument();
    expect(onLogin).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Verification code')).toHaveAttribute('aria-invalid', 'true');
  });

  it('binds the existing account and refreshes its identity', async () => {
    const onSuccess = vi.fn().mockResolvedValue(undefined);
    vi.mocked(bindPhone).mockResolvedValueOnce({ message: 'Phone bound.' });
    render(<PhoneAuthForm purpose="bind" onSuccess={onSuccess} />);
    fireEvent.click(screen.getByLabelText('Country code'));
    fireEvent.click(screen.getByText('United Kingdom'));
    enterPhone('7911123456');
    fireEvent.change(screen.getByLabelText('Verification code'), { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: 'Bind mobile number' }));
    expect(await screen.findByText('Phone bound.')).toBeInTheDocument();
    expect(bindPhone).toHaveBeenCalledWith({ phone: '+447911123456', code: '123456' });
    expect(onSuccess).toHaveBeenCalledOnce();
  });

  it('renders request errors and allows another attempt', async () => {
    vi.mocked(sendPhoneCode).mockRejectedValueOnce(new Error('SMS unavailable.'));
    render(<PhoneAuthForm purpose="login" onLogin={vi.fn()} />);
    enterPhone();
    fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('SMS unavailable.');
    expect(screen.getByRole('button', { name: 'Send verification code' })).toBeEnabled();
  });

  it('requires captcha and replaces the consumed widget after an unsuccessful send', async () => {
    type WidgetOptions = Parameters<NonNullable<typeof window.turnstile>['render']>[1];
    const widgetOptions: WidgetOptions[] = [];
    const remove = vi.fn();
    window.turnstile = {
      render: vi.fn((_container, options) => {
        widgetOptions.push(options);
        return String(widgetOptions.length);
      }),
      remove,
    };
    vi.mocked(sendPhoneCode).mockRejectedValueOnce(new Error('Try later.'));
    render(
      <SiteConfigProvider
        initialConfig={{
          ...buildTimeSiteConfig,
          branding: { ...buildTimeSiteConfig.branding, turnstileSiteKey: 'site-key' },
        }}
      >
        <PhoneAuthForm purpose="login" onLogin={vi.fn()} />
      </SiteConfigProvider>,
    );
    enterPhone();
    fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));
    expect(await screen.findByText('Please complete the captcha.')).toBeInTheDocument();
    expect(sendPhoneCode).not.toHaveBeenCalled();
    act(() => widgetOptions[0].callback('captcha-token'));
    fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));
    expect(await screen.findByText('Try later.')).toBeInTheDocument();
    expect(sendPhoneCode).toHaveBeenCalledWith(
      expect.objectContaining({ turnstileToken: 'captcha-token' }),
    );
    await waitFor(() => expect(widgetOptions).toHaveLength(2));
    expect(remove).toHaveBeenCalledWith('1');
    fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));
    expect(await screen.findByText('Please complete the captcha.')).toBeInTheDocument();
    expect(sendPhoneCode).toHaveBeenCalledTimes(1);
  });
});
