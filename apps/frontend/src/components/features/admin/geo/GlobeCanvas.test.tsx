// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { GeoAnalyticsResponse } from '@/lib/api/admin';
import { CONTINENT_COLORS } from './geoMath';
import { GlobeCanvas, type PreparedAtlas } from './GlobeCanvas';

const china = {
  type: 'Feature',
  properties: { id: 'CHN', name: 'China' },
  geometry: { type: 'Point', coordinates: [104, 35] },
} as PreparedAtlas['countries'][number];

const atlas: PreparedAtlas = {
  countries: [china],
  byId: new Map([['CHN', china]]),
  coordinates: new Map([['CHN', [104, 35]]]),
};

function response(outputTokens = 8): GeoAnalyticsResponse {
  return {
    meta: {
      source: 'api_logs',
      generated_at: '2026-07-16T00:00:00Z',
      start: '2026-07-16T00:00:00Z',
      hours: 1,
      rows_total: 12,
      rows_with_ip: 12,
      geoip: { country: true, provider: 'dbip-lite', attribution: null },
      degraded: false,
      degraded_reasons: [],
      unmapped_alpha2: [],
      notes: [],
    },
    bucket_cols: ['c', 'cc', 'cont', 'n', 'err', 'users', 'tin', 'tout', 'gs', 'p50', 'p90'],
    hours_index: ['2026-07-16T00:00:00Z'],
    hours: [
      {
        b: [['CHN', 'CN', 'AS', 12, 0, 3, 20, outputTokens, 2, 100, 200]],
      },
    ],
  };
}

class ResizeObserverMock {
  observe() {}
  disconnect() {}
  unobserve() {}
}

function renderGlobe(onSelect = vi.fn(), metric: 'n' | 'tout' | 'gs' = 'n', data = response()) {
  render(
    <GlobeCanvas
      data={data}
      atlas={atlas}
      hourIndex={0}
      metric={metric}
      viewRequest={{ id: 1, rotation: [-104, -35] }}
      onSelect={onSelect}
    />,
  );
  return { onSelect };
}

function installPointerCapture(globe: SVGSVGElement) {
  const captured = new Set<number>();
  const setPointerCapture = vi.fn((pointerId: number) => captured.add(pointerId));
  const releasePointerCapture = vi.fn((pointerId: number) => captured.delete(pointerId));
  Object.defineProperties(globe, {
    setPointerCapture: { configurable: true, value: setPointerCapture },
    hasPointerCapture: {
      configurable: true,
      value: (pointerId: number) => captured.has(pointerId),
    },
    releasePointerCapture: { configurable: true, value: releasePointerCapture },
  });
  return { setPointerCapture, releasePointerCapture };
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ResizeObserverMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('GlobeCanvas', () => {
  it('renders only continent-colored request origins with accessible child controls', () => {
    renderGlobe();

    const globe = screen.getByRole('group', {
      name: 'Globe of IP-based request origins and day and night',
    });
    const origin = screen.getByRole('button', { name: 'China request origin' });

    expect(origin).toHaveAttribute('tabindex', '0');
    expect(origin.querySelector('circle')).toHaveAttribute('fill', CONTINENT_COLORS.AS);
    expect(globe.querySelector('[data-layer="providers"]')).not.toBeInTheDocument();
    expect(globe.querySelector('[data-layer="arcs"]')).not.toBeInTheDocument();
    expect(document.querySelector('.geo-demand-flow')).not.toBeInTheDocument();
  });

  it('lets a real pointer tap select a country without capturing the pointer', () => {
    const { onSelect } = renderGlobe();
    const globe = screen.getByRole('group', {
      name: 'Globe of IP-based request origins and day and night',
    }) as unknown as SVGSVGElement;
    const origin = screen.getByRole('button', { name: 'China request origin' });
    const { setPointerCapture } = installPointerCapture(globe);

    fireEvent.pointerDown(origin, {
      button: 0,
      pointerId: 7,
      pointerType: 'mouse',
      clientX: 100,
      clientY: 100,
    });
    fireEvent.pointerUp(origin, {
      pointerId: 7,
      pointerType: 'mouse',
      clientX: 100,
      clientY: 100,
    });
    fireEvent.click(origin);

    expect(setPointerCapture).not.toHaveBeenCalled();
    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect).toHaveBeenCalledWith({ type: 'country', country: 'CHN' });
  });

  it('captures only after a drag and suppresses its trailing click', () => {
    const { onSelect } = renderGlobe();
    const globe = screen.getByRole('group', {
      name: 'Globe of IP-based request origins and day and night',
    }) as unknown as SVGSVGElement;
    const origin = screen.getByRole('button', { name: 'China request origin' });
    const { setPointerCapture, releasePointerCapture } = installPointerCapture(globe);

    fireEvent.pointerDown(origin, {
      button: 0,
      pointerId: 8,
      pointerType: 'mouse',
      clientX: 100,
      clientY: 100,
    });
    fireEvent.pointerMove(origin, {
      pointerId: 8,
      pointerType: 'mouse',
      clientX: 130,
      clientY: 110,
    });
    fireEvent.pointerUp(origin, {
      pointerId: 8,
      pointerType: 'mouse',
      clientX: 130,
      clientY: 110,
    });
    fireEvent.click(origin);

    expect(setPointerCapture).toHaveBeenCalledOnce();
    expect(releasePointerCapture).toHaveBeenCalledOnce();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it('clears a pending drag when the pointer is released outside the globe', () => {
    const { onSelect } = renderGlobe();
    const globe = screen.getByRole('group', {
      name: 'Globe of IP-based request origins and day and night',
    }) as unknown as SVGSVGElement;
    const origin = screen.getByRole('button', { name: 'China request origin' });
    const { setPointerCapture } = installPointerCapture(globe);

    fireEvent.pointerDown(origin, {
      button: 0,
      pointerId: 9,
      pointerType: 'mouse',
      clientX: 100,
      clientY: 100,
    });
    fireEvent.pointerUp(window, {
      pointerId: 9,
      pointerType: 'mouse',
      clientX: 102,
      clientY: 100,
    });
    fireEvent.pointerMove(globe, {
      buttons: 0,
      pointerId: 9,
      pointerType: 'mouse',
      clientX: 140,
      clientY: 100,
    });
    fireEvent.click(origin);

    expect(setPointerCapture).not.toHaveBeenCalled();
    expect(onSelect).toHaveBeenCalledOnce();
  });

  it('does not render a heat bubble when the selected metric is zero', () => {
    renderGlobe(vi.fn(), 'tout', response(0));

    expect(screen.queryByRole('button', { name: 'China request origin' })).not.toBeInTheDocument();
    expect(document.querySelector('g[data-layer="heat"]')).toBeEmptyDOMElement();
  });
});
