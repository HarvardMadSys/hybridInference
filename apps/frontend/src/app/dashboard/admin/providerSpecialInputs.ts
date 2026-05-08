export type ProviderSpecialInputKind = 'cookieHeader';

export interface ProviderSpecialInputConfig {
  kind: ProviderSpecialInputKind;
  actionLabel: string;
  title: string;
  helpText: string;
  placeholder: string;
  normalize: (input: string) => string;
}

const CHATGPT_SESSION_COOKIE_NAMES = [
  '__Secure-next-auth.session-token',
  'next-auth.session-token',
];

const OPTIONAL_CHATGPT_COOKIE_NAMES = ['cf_clearance', '__cf_bm'];

interface ParsedCookiePair {
  name: string;
  value: string;
}

function parseCookiePairs(cookieText: string): ParsedCookiePair[] {
  const pairs = cookieText
    .split(';')
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const index = part.indexOf('=');
      if (index <= 0) return null;
      return {
        name: part.slice(0, index).trim(),
        value: part.slice(index + 1).trim(),
      };
    });

  if (pairs.length === 0 || pairs.some((pair) => pair === null)) {
    throw new Error('Cookie header must contain name=value pairs.');
  }

  return pairs as ParsedCookiePair[];
}

function stripCookieHeaderPrefix(input: string): string {
  return input.replace(/^cookie\s*:\s*/i, '').trim();
}

function isHeaderLike(input: string): boolean {
  return /^cookie\s*:/i.test(input) || input.includes(';');
}

function isChatGPTSessionCookieName(name: string): boolean {
  return CHATGPT_SESSION_COOKIE_NAMES.some((sessionName) => {
    if (name === sessionName) return true;

    const chunkPrefix = `${sessionName}.`;
    return name.startsWith(chunkPrefix) && /^\d+$/.test(name.slice(chunkPrefix.length));
  });
}

export function normalizeChatGPTCookieInput(input: string): string {
  const trimmed = input.trim();
  if (!trimmed) {
    throw new Error('Cookie value must not be blank.');
  }

  if (!isHeaderLike(trimmed)) {
    return trimmed;
  }

  const pairs = parseCookiePairs(stripCookieHeaderPrefix(trimmed));
  const selected = pairs.filter(
    (pair) =>
      isChatGPTSessionCookieName(pair.name) || OPTIONAL_CHATGPT_COOKIE_NAMES.includes(pair.name),
  );
  const hasSession = selected.some((pair) => isChatGPTSessionCookieName(pair.name));
  if (!hasSession) {
    throw new Error('ChatGPT cookie header did not include a session cookie.');
  }

  return selected.map((pair) => `${pair.name}=${pair.value}`).join('; ');
}

export const providerSpecialInputs: Record<string, ProviderSpecialInputConfig> = {
  chatgpt: {
    kind: 'cookieHeader',
    actionLabel: 'Upload cookie',
    title: 'Paste ChatGPT cookie',
    helpText: 'Paste either a raw credential string or a full browser Cookie header.',
    placeholder:
      'Cookie: __Secure-next-auth.session-token=...; cf_clearance=...\n\n' +
      'or paste an existing raw credential string',
    normalize: normalizeChatGPTCookieInput,
  },
};

export function getProviderSpecialInput(provider: string): ProviderSpecialInputConfig | undefined {
  return providerSpecialInputs[provider];
}
