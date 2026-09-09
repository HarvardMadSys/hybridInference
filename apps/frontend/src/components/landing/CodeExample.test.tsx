// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import { CodeExample } from './CodeExample';

describe('CodeExample', () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('hides the quickstart when the distribution omits an API base', () => {
    render(
      <SiteConfigProvider initialConfig={buildTimeSiteConfig}>
        <CodeExample />
      </SiteConfigProvider>,
    );

    expect(screen.queryByText('Quickstart')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument();
  });

  it('copies the deployment-specific quickstart shown on the landing page', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const config = {
      ...buildTimeSiteConfig,
      branding: {
        ...buildTimeSiteConfig.branding,
        exampleApiBase: 'https://staging.freeinference.org',
        exampleApiKeyEnvVar: 'FREEINFERENCE_API_KEY',
        exampleModel: 'glm-5.1',
      },
    };

    render(
      <SiteConfigProvider initialConfig={config}>
        <CodeExample />
      </SiteConfigProvider>,
    );

    const command = screen.getByText(
      /curl 'https:\/\/staging.freeinference.org\/v1\/chat\/completions'/,
    );
    expect(command).toHaveTextContent('"model": "glm-5.1"');
    expect(command).toHaveTextContent('Bearer $FREEINFERENCE_API_KEY');
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(command.textContent));
    expect(screen.getByRole('button', { name: 'Copied!' })).toBeInTheDocument();
  });
});
