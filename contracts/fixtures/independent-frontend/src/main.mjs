import { createControlClient } from './client.mjs';

const form = document.querySelector('#login-form');
const output = document.querySelector('#output');

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  output.textContent = 'Checking contract…';
  const client = createControlClient({
    baseUrl: document.querySelector('#base-url').value,
  });
  try {
    const login = await client.login(
      document.querySelector('#email').value,
      document.querySelector('#password').value,
    );
    const capabilities = await client.capabilities();
    const session = await client.session();
    const models = await client.models();
    const apiKey = await client.createApiKey();
    const model = models.data?.[0]?.id;
    if (!model) throw new Error('No model is available for the streaming smoke');
    let streamedText = '';
    for await (const chunk of client.streamChat({
      model,
      messages: [{ role: 'user', content: 'Reply with: contract-ok' }],
    })) {
      streamedText += chunk.choices?.[0]?.delta?.content || '';
    }
    output.textContent = JSON.stringify(
      {
        user: session.user || session || login.user,
        capabilities,
        model_count: models.data?.length ?? 0,
        api_key_prefix: apiKey.key_prefix,
        streamed_text: streamedText,
      },
      null,
      2,
    );
  } catch (error) {
    output.textContent = JSON.stringify(
      {
        code: error.code || 'UNEXPECTED_ERROR',
        message: error.message,
        request_id: error.requestId || null,
      },
      null,
      2,
    );
  }
});
