export class ControlError extends Error {
  constructor(code, message, status, requestId = null) {
    super(message);
    this.name = 'ControlError';
    this.code = code;
    this.status = status;
    this.requestId = requestId;
  }
}

function normalizedBaseUrl(baseUrl) {
  return baseUrl.replace(/\/+$/, '');
}

async function decodeError(response) {
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    return new ControlError(
      `HTTP_${response.status}`,
      `HTTP ${response.status}: ${response.statusText}`,
      response.status,
    );
  }

  const stable = payload?.error;
  const code =
    (typeof stable?.code === 'string' && stable.code) ||
    (typeof payload?.error_code === 'string' && payload.error_code) ||
    `HTTP_${response.status}`;
  const message =
    (typeof stable?.message === 'string' && stable.message) ||
    (typeof payload?.message === 'string' && payload.message) ||
    (typeof payload?.detail === 'string' && payload.detail) ||
    `HTTP ${response.status}`;
  return new ControlError(code, message, response.status, payload?.request_id ?? null);
}

function takeSseLine(buffer, flush) {
  for (let index = 0; index < buffer.length; index += 1) {
    const character = buffer[index];
    if (character === '\n') {
      return { line: buffer.slice(0, index), rest: buffer.slice(index + 1) };
    }
    if (character !== '\r') continue;
    if (index + 1 === buffer.length && !flush) return null;
    const delimiterLength = buffer[index + 1] === '\n' ? 2 : 1;
    return {
      line: buffer.slice(0, index),
      rest: buffer.slice(index + delimiterLength),
    };
  }
  return null;
}

function eventData(lines) {
  const values = [];
  for (const line of lines) {
    if (line === 'data') {
      values.push('');
    } else if (line.startsWith('data:')) {
      const value = line.slice(5);
      values.push(value.startsWith(' ') ? value.slice(1) : value);
    }
  }
  return values.join('\n');
}

export function createControlClient({ baseUrl = '', fetchImpl = globalThis.fetch } = {}) {
  const root = normalizedBaseUrl(baseUrl);
  let accessToken = null;
  let apiKey = null;

  async function request(path, init = {}, token = accessToken) {
    const headers = new Headers(init.headers || {});
    if (token) headers.set('Authorization', `Bearer ${token}`);
    const response = await fetchImpl(`${root}${path}`, {
      ...init,
      headers,
      credentials: 'include',
    });
    if (!response.ok) throw await decodeError(response);
    return response;
  }

  return {
    async capabilities() {
      return (await request('/capabilities')).json();
    },

    async login(email, password) {
      const response = await request('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      });
      const payload = await response.json();
      accessToken = payload.access_token;
      return payload;
    },

    async models() {
      return (await request('/user/models')).json();
    },

    async session() {
      return (await request('/user/me')).json();
    },

    async createApiKey() {
      const payload = await (await request('/user/api-keys', { method: 'POST' })).json();
      apiKey = payload.api_key;
      return payload;
    },

    async *streamChat({ model, messages }) {
      if (!apiKey) {
        throw new ControlError(
          'MISSING_API_KEY',
          'Create an API key before calling the inference protocol',
          401,
        );
      }
      const response = await request(
        '/v1/chat/completions',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model, messages, stream: true }),
        },
        apiKey,
      );
      if (!response.headers.get('content-type')?.startsWith('text/event-stream')) {
        throw new ControlError(
          'INVALID_SSE_CONTENT_TYPE',
          'Streaming response is not text/event-stream',
          502,
        );
      }
      if (!response.body) throw new ControlError('EMPTY_STREAM', 'Response has no body', 502);

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let eventLines = [];
      while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        while (true) {
          const parsed = takeSseLine(buffer, done);
          if (!parsed) break;
          buffer = parsed.rest;
          if (parsed.line !== '') {
            eventLines.push(parsed.line);
            continue;
          }
          const data = eventData(eventLines);
          eventLines = [];
          if (data === '[DONE]') return;
          if (data) yield JSON.parse(data);
        }
        if (done) break;
      }
      throw new ControlError('INVALID_SSE_TERMINATION', 'Stream ended without [DONE]', 502);
    },
  };
}
