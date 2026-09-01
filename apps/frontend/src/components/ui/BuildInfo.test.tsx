// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/config/env', () => ({
  config: { buildSha: 'abcdef1234567890', buildTimestamp: '' },
}));

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useBranding: () => ({ commitUrlBase: 'https://github.com/example/runtime/commit' }),
}));

import { BuildInfo } from './BuildInfo';

describe('BuildInfo', () => {
  afterEach(() => cleanup());

  it('links build identity through the runtime repository URL', () => {
    render(<BuildInfo />);

    expect(screen.getByRole('link', { name: 'abcdef1' })).toHaveAttribute(
      'href',
      'https://github.com/example/runtime/commit/abcdef1234567890',
    );
  });
});
