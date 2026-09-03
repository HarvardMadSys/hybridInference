import type { Branding } from '@/config/branding';

export type QuickstartBranding = Pick<
  Branding,
  'exampleApiBase' | 'exampleApiKeyEnvVar' | 'exampleModel'
>;

function shellSingleQuote(value: string): string {
  const singleQuote = String.fromCodePoint(39);
  const escapedSingleQuote = `${singleQuote}"${singleQuote}"${singleQuote}`;

  return `${singleQuote}${value.replaceAll(singleQuote, escapedSingleQuote)}${singleQuote}`;
}

export function buildCurlExample(branding: QuickstartBranding): string {
  const endpoint = `${branding.exampleApiBase}/v1/chat/completions`;
  const payload = JSON.stringify(
    {
      model: branding.exampleModel,
      messages: [{ role: 'user', content: 'Hello!' }],
    },
    null,
    2,
  );

  return `curl ${shellSingleQuote(endpoint)} \\
  -H "Authorization: Bearer $${branding.exampleApiKeyEnvVar}" \\
  -H "Content-Type: application/json" \\
  -d ${shellSingleQuote(payload)}`;
}

/** Prefer the configured example model when the gateway serves it, else the first served model. */
export function pickExampleModel(models: readonly string[] | null, preferred: string): string {
  if (!models || models.length === 0) return preferred;
  return models.includes(preferred) ? preferred : models[0];
}
