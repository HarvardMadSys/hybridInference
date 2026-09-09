import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { SITE_CONFIG_ERROR_DIGEST } from '@/config/site-config-error';
import GlobalError from './global-error';

describe('GlobalError', () => {
  it('renders a standalone, retryable configuration error without private details', () => {
    // Production strips the message but preserves the server error digest.
    const error = Object.assign(new Error('http://private-backend:8080 secret-value'), {
      digest: SITE_CONFIG_ERROR_DIGEST,
    });
    const html = renderToStaticMarkup(<GlobalError error={error} reset={vi.fn()} />);

    expect(html).toContain('<html lang="en">');
    expect(html).toContain('<body');
    expect(html).toContain('Site configuration could not be loaded');
    expect(html).toContain('href=""');
    expect(html).toContain('Retry');
    expect(html).not.toContain('private-backend');
    expect(html).not.toContain('secret-value');
    expect(html).not.toContain('/signup');
  });

  it('does not mislabel unrelated root errors as configuration failures', () => {
    const html = renderToStaticMarkup(
      <GlobalError error={new Error('unrelated private error')} reset={vi.fn()} />,
    );

    expect(html).toContain('Unable to load the site');
    expect(html).not.toContain('Site configuration could not be loaded');
    expect(html).not.toContain('unrelated private error');
  });
});
