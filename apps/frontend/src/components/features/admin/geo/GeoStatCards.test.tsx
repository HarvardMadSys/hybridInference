// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type { CurrentHourStats } from './geoMath';
import { GeoStatCards } from './GeoStatCards';

afterEach(cleanup);

function zeroMetricStats(): CurrentHourStats {
  return {
    totalRequests: 6,
    locatedRequests: 6,
    locatedFraction: 1,
    unlocatedFraction: 0,
    activeCountries: 2,
    activeContinents: 2,
    continentTotals: [],
    topContinent: null,
    demandComplementarity: 0,
    observedContinents: 2,
  };
}

describe('GeoStatCards', () => {
  it('describes a zero selected metric without inventing a single-continent mix', () => {
    render(<GeoStatCards stats={zeroMetricStats()} />);

    expect(screen.getByText('no located volume for selected metric')).toBeInTheDocument();
    expect(screen.queryByText('single-continent hour')).not.toBeInTheDocument();
  });

  it('describes all-unlocated demand without claiming volume is absent', () => {
    render(
      <GeoStatCards
        stats={{
          ...zeroMetricStats(),
          locatedRequests: 0,
          locatedFraction: 0,
          unlocatedFraction: 1,
          activeCountries: 0,
          activeContinents: 0,
        }}
      />,
    );

    expect(screen.getByText('no located volume for selected metric')).toBeInTheDocument();
  });
});
