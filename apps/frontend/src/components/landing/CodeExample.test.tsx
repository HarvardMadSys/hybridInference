// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import { CodeExample } from './CodeExample';

describe('CodeExample', () => {
  afterEach(cleanup);

  it('hides the quickstart when the distribution omits an API base', () => {
    const config = {
      ...buildTimeSiteConfig,
      branding: {
        ...buildTimeSiteConfig.branding,
        exampleApiBase: '',
      },
    };

    render(
      <SiteConfigProvider initialConfig={config}>
        <CodeExample />
      </SiteConfigProvider>,
    );

    expect(screen.queryByText('Quickstart')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument();
  });

  it('renders a copyable quickstart for a configured API base', () => {
    render(
      <SiteConfigProvider initialConfig={buildTimeSiteConfig}>
        <CodeExample />
      </SiteConfigProvider>,
    );

    expect(screen.getByText('Quickstart')).toBeInTheDocument();
    expect(
      screen.getByText(/curl http:\/\/localhost:8080\/v1\/chat\/completions/),
    ).toBeInTheDocument();
  });
});
