// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { formatLatency } from './format';
import { RequestVolumeCard } from './RequestVolumeCard';

afterEach(cleanup);

describe('the request volume card', () => {
  it('shows the requests of a window, how they ended and the average latency', () => {
    const { container } = render(
      <RequestVolumeCard
        label="Last hour"
        total={1234}
        ok={1200}
        failed={34}
        averageLatencyMs={850}
      />,
    );

    expect(screen.getByText('Last hour')).toBeInTheDocument();
    expect(screen.getByText((1234).toLocaleString())).toBeInTheDocument();
    expect(container).toHaveTextContent(`${(1200).toLocaleString()} ok`);
    expect(container).toHaveTextContent('34 err');
    expect(container).toHaveTextContent('850ms avg');
  });

  it("takes the page's own lines, its own number format and what it draws below", () => {
    const { container } = render(
      <RequestVolumeCard
        label="Last day"
        total={2000}
        ok={1990}
        failed={10}
        averageLatencyMs={null}
        afterFailed={<div>3 disc</div>}
        afterAverage={<div>7 batch</div>}
        formatNumber={(value) => `n${value}`}
      >
        <div data-testid="bars" />
      </RequestVolumeCard>,
    );

    expect(screen.getByText('n2000')).toBeInTheDocument();
    // In the order of the column: failed, the page's line, the latency, the page's line.
    expect(container).toHaveTextContent(/n10 err3 disc— avg7 batch/);
    expect(screen.getByTestId('bars')).toBeInTheDocument();
  });
});

describe('a latency', () => {
  it.each([
    [null, '—'],
    [undefined, '—'],
    [0, '0ms'],
    [849.6, '850ms'],
    [1000, '1.0s'],
    [12345, '12.3s'],
  ])('%s reads %s', (ms, text) => {
    expect(formatLatency(ms)).toBe(text);
  });
});
