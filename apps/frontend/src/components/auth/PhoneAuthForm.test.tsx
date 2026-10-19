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

function selectCountry(name: string) {
  fireEvent.click(screen.getByRole('button', { name: 'Country code' }));
  // Narrow the list first, as a user would: getByRole computes the accessible
  // name of every option it considers, and over the full list of ~250 that
  // took seconds per call on a loaded CI runner -- past vitest's 5s timeout.
  fireEvent.change(screen.getByLabelText('Search country or code'), { target: { value: name } });
  fireEvent.click(screen.getByRole('option', { name: new RegExp(`^${name}\\s*\\+\\d+$`) }));
}

function enterSignupDetails() {
  fireEvent.change(screen.getByLabelText('Verification code'), { target: { value: '123456' } });
  fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'Phone User' } });
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

describe('signup country code selection', () => {
  it('localizes the country picker and national-number guidance', () => {
    wording.messages['auth.phone.country_code_label'] = '国家或地区区号';
    wording.messages['auth.phone.country_code_options_label'] = '可选区号';
    wording.messages['auth.phone.country_search'] = '搜索国家或区号';
    wording.messages['auth.phone.national_hint'] = '先选择区号，再输入手机号码。';
    render(<PhoneAuthForm purpose="signup" onSuccess={vi.fn()} />);

    const picker = screen.getByRole('button', { name: '国家或地区区号' });
    expect(picker).toHaveAttribute('aria-haspopup', 'listbox');
    expect(picker).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText('先选择区号，再输入手机号码。')).toBeInTheDocument();
    fireEvent.click(picker);
    expect(picker).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('listbox', { name: '可选区号' })).toBeInTheDocument();
    expect(screen.getByLabelText('搜索国家或区号')).toBeInTheDocument();
  });

  it('supports arrow navigation and returns focus to the picker on Escape', () => {
    render(<PhoneAuthForm purpose="signup" onSuccess={vi.fn()} />);
    const picker = screen.getByRole('button', { name: 'Country code' });

    fireEvent.keyDown(picker, { key: 'ArrowDown' });
    const search = screen.getByLabelText('Search country or code');
    expect(search).toHaveFocus();
    fireEvent.change(search, { target: { value: 'Latvia' } });
    fireEvent.keyDown(search, { key: 'ArrowDown' });
    expect(screen.getByRole('option', { name: /^Latvia\s*\+371$/ })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });

    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(picker).toHaveFocus();
    expect(picker).toHaveAttribute('aria-expanded', 'false');
  });

  it.each(['13812345678', '138 1234-5678'])(
    'sends the same international number for verification and signup from %s',
    async (nationalNumber) => {
      vi.mocked(sendPhoneCode).mockResolvedValueOnce({ message: 'Code sent.', retry_after: 60 });
      vi.mocked(signupPhone).mockResolvedValueOnce({
        email: null,
        phone: '+8613812345678',
        user_id: 'phone-user',
        requires_approval: false,
        message: 'Account created successfully. You can now log in.',
      });
      const onSuccess = vi.fn();
      render(<PhoneAuthForm purpose="signup" onSuccess={onSuccess} />);

      selectCountry('China');
      expect(screen.getByRole('button', { name: 'Country code' })).toHaveTextContent('🇨🇳');
      expect(screen.getByRole('button', { name: 'Country code' })).toHaveTextContent('+86');
      enterPhone(nationalNumber);
      fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));
      expect(await screen.findByText('Code sent.')).toBeInTheDocument();
      expect(sendPhoneCode).toHaveBeenCalledWith({
        phone: '+8613812345678',
        purpose: 'signup',
        turnstileToken: undefined,
      });

      enterSignupDetails();
      fireEvent.click(screen.getByRole('button', { name: 'Sign Up' }));
      await waitFor(() => expect(onSuccess).toHaveBeenCalledOnce());
      expect(signupPhone).toHaveBeenCalledWith({
        phone: '+8613812345678',
        code: '123456',
        user_name: 'Phone User',
        use_case: undefined,
        accepted_tos: true,
      });
    },
  );

  it('splits a pasted international number into the matching country and national number', async () => {
    vi.mocked(sendPhoneCode).mockResolvedValueOnce({ message: 'Code sent.', retry_after: 60 });
    render(<PhoneAuthForm purpose="signup" onSuccess={vi.fn()} />);
    selectCountry('China');

    enterPhone('+44 7911 123456');

    expect(screen.getByRole('button', { name: 'Country code' })).toHaveTextContent('🇬🇧');
    expect(screen.getByRole('button', { name: 'Country code' })).toHaveTextContent('+44');
    expect(screen.getByLabelText('Mobile number')).toHaveValue('7911123456');
    fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));
    await waitFor(() =>
      expect(sendPhoneCode).toHaveBeenCalledWith(
        expect.objectContaining({ phone: '+447911123456', purpose: 'signup' }),
      ),
    );
  });

  it('clears a verification code when selecting another country', async () => {
    render(<PhoneAuthForm purpose="signup" onSuccess={vi.fn()} />);
    selectCountry('China');
    enterPhone();
    enterSignupDetails();

    selectCountry('United Kingdom');

    expect(screen.getByLabelText('Mobile number')).toHaveValue('13812345678');
    expect(screen.getByLabelText('Verification code')).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: 'Sign Up' }));
    expect(await screen.findByText('Enter the six-digit verification code.')).toBeInTheDocument();
    expect(signupPhone).not.toHaveBeenCalled();
  });

  it('accepts a pasted country code beyond the original shortlist for verification and signup', async () => {
    vi.mocked(sendPhoneCode).mockResolvedValueOnce({ message: 'Code sent.', retry_after: 60 });
    vi.mocked(signupPhone).mockResolvedValueOnce({
      email: null,
      phone: '+37121234567',
      user_id: 'phone-user',
      requires_approval: false,
      message: 'Account created successfully. You can now log in.',
    });
    const onSuccess = vi.fn();
    render(<PhoneAuthForm purpose="signup" onSuccess={onSuccess} />);
    enterPhone('+37121234567');

    expect(screen.getByRole('button', { name: 'Country code' })).toHaveTextContent('🇱🇻');
    expect(screen.getByRole('button', { name: 'Country code' })).toHaveTextContent('+371');
    expect(screen.getByLabelText('Mobile number')).toHaveValue('21234567');
    fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));
    expect(await screen.findByText('Code sent.')).toBeInTheDocument();
    expect(sendPhoneCode).toHaveBeenCalledWith(
      expect.objectContaining({ phone: '+37121234567', purpose: 'signup' }),
    );

    enterSignupDetails();
    fireEvent.click(screen.getByRole('button', { name: 'Sign Up' }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledOnce());
    expect(signupPhone).toHaveBeenCalledWith(
      expect.objectContaining({ phone: '+37121234567', code: '123456' }),
    );
  });

  it('preserves a selected country when another country shares its calling code', async () => {
    vi.mocked(sendPhoneCode).mockResolvedValueOnce({ message: 'Code sent.', retry_after: 60 });
    render(<PhoneAuthForm purpose="signup" onSuccess={vi.fn()} />);

    selectCountry('Canada');
    enterPhone('416 555 0123');
    const picker = screen.getByRole('button', { name: 'Country code' });
    expect(picker).toHaveTextContent('🇨🇦');
    expect(picker).toHaveTextContent('+1');

    enterPhone('+1 647 555 0123');
    expect(picker).toHaveTextContent('🇨🇦');
    expect(screen.getByLabelText('Mobile number')).toHaveValue('6475550123');
    fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));
    expect(await screen.findByText('Code sent.')).toBeInTheDocument();
    expect(sendPhoneCode).toHaveBeenCalledWith(
      expect.objectContaining({ phone: '+16475550123', purpose: 'signup' }),
    );
  });

  it.each(['Latvia', 'LV', '+371'])(
    'finds a country by its name, ISO code or calling code: %s',
    (query) => {
      render(<PhoneAuthForm purpose="signup" onSuccess={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: 'Country code' }));
      fireEvent.change(screen.getByLabelText('Search country or code'), {
        target: { value: query },
      });

      expect(screen.getByRole('option', { name: /^Latvia\s*\+371$/ })).toBeInTheDocument();
      expect(screen.queryByRole('option', { name: /^China\s*\+86$/ })).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole('option', { name: /^Latvia\s*\+371$/ }));
      expect(screen.getByRole('button', { name: 'Country code' })).toHaveTextContent('🇱🇻');
      expect(screen.getByRole('button', { name: 'Country code' })).toHaveTextContent('+371');
    },
  );

  it('preserves a non-geographic international number for verification and signup', async () => {
    vi.mocked(sendPhoneCode).mockResolvedValueOnce({ message: 'Code sent.', retry_after: 60 });
    vi.mocked(signupPhone).mockResolvedValueOnce({
      email: null,
      phone: '+80012345678',
      user_id: 'phone-user',
      requires_approval: false,
      message: 'Account created successfully. You can now log in.',
    });
    const onSuccess = vi.fn();
    render(<PhoneAuthForm purpose="signup" onSuccess={onSuccess} />);
    selectCountry('China');
    enterPhone('+800 1234 5678');

    expect(screen.getByRole('button', { name: 'Country code' })).toHaveTextContent('🌐');
    expect(screen.getByLabelText('Mobile number')).toHaveValue('80012345678');
    fireEvent.click(screen.getByRole('button', { name: 'Country code' }));
    expect(
      screen.getByRole('option', { name: /^International\s*\+$/, selected: true }),
    ).toBeInTheDocument();
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));
    expect(await screen.findByText('Code sent.')).toBeInTheDocument();
    expect(sendPhoneCode).toHaveBeenCalledWith(
      expect.objectContaining({ phone: '+80012345678', purpose: 'signup' }),
    );

    enterSignupDetails();
    fireEvent.click(screen.getByRole('button', { name: 'Sign Up' }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledOnce());
    expect(signupPhone).toHaveBeenCalledWith(
      expect.objectContaining({ phone: '+80012345678', code: '123456' }),
    );
  });

  it('still validates the full E164 length in international mode', async () => {
    render(<PhoneAuthForm purpose="signup" onSuccess={vi.fn()} />);
    enterPhone('+8001234567890123');
    fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));

    expect(
      await screen.findByText('Enter a phone number with its country code.'),
    ).toBeInTheDocument();
    expect(sendPhoneCode).not.toHaveBeenCalled();
    enterSignupDetails();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Sign Up' }));
    });
    expect(signupPhone).not.toHaveBeenCalled();
  });

  it.each(['12', '12345678901234', '1381234x5678'])(
    'rejects an invalid combined international number before sending or signing up: %s',
    async (nationalNumber) => {
      render(<PhoneAuthForm purpose="signup" onSuccess={vi.fn()} />);
      selectCountry('China');
      enterPhone(nationalNumber);
      fireEvent.click(screen.getByRole('button', { name: 'Send verification code' }));

      expect(
        await screen.findByText('Enter a phone number with its country code.'),
      ).toBeInTheDocument();
      expect(screen.getByLabelText('Mobile number')).toHaveAttribute('aria-invalid', 'true');
      expect(sendPhoneCode).not.toHaveBeenCalled();

      enterSignupDetails();
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Sign Up' }));
      });
      expect(screen.getByLabelText('Mobile number')).toHaveAttribute('aria-invalid', 'true');
      expect(signupPhone).not.toHaveBeenCalled();
    },
  );
});
