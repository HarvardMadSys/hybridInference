// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { GeoAnalyticsResponse } from '@/lib/api/admin';
import {
  ORIGIN_COLORS,
  deriveGeoMetricModel,
  hourOriginSummary,
  windowOriginSummary,
} from './geoMath';
import { GlobeCanvas, type GlobeViewRequest, type PreparedAtlas } from './GlobeCanvas';

const china = {
  type: 'Feature',
  properties: { id: 'CHN', name: 'China' },
  geometry: { type: 'Point', coordinates: [104, 35] },
} as PreparedAtlas['countries'][number];

// One stable array: a new land layer is a new globe, and rebuilds it.
const NO_LAND: [number, number][] = [];

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
    bucket_cols: ['c', 'cont', 'n', 'tout'],
    hours_index: ['2026-07-16T00:00:00Z'],
    hours: [
      {
        b: [['CHN', 'AS', 12, outputTokens]],
      },
    ],
  };
}

class ResizeObserverMock {
  observe() {}
  disconnect() {}
  unobserve() {}
}

function renderGlobe(
  onSelect = vi.fn(),
  metric: 'n' | 'tout' = 'n',
  data = response(),
  selectedCountry: string | null = null,
  mode: 'window' | 'hour' = 'hour',
) {
  const metricModel = deriveGeoMetricModel(data, metric);
  const firstView: GlobeViewRequest = { id: 1, rotation: [-104, -35] };
  const renderCanvas = (
    hourIndex: number,
    nextSelectedCountry: string | null,
    viewRequest: GlobeViewRequest = firstView,
  ) => (
    <GlobeCanvas
      atlas={atlas}
      land={NO_LAND}
      metricModel={metricModel}
      mode={mode}
      viewRequest={viewRequest}
      onSelect={onSelect}
      selectedCountry={nextSelectedCountry}
      snapshotTimestamp={data.hours_index[hourIndex]}
      summary={
        mode === 'window'
          ? windowOriginSummary(data, metricModel)
          : hourOriginSummary(data, hourIndex, metricModel)
      }
    />
  );
  const view = render(renderCanvas(0, selectedCountry));
  return {
    ...view,
    metricModel,
    onSelect,
    rerenderAt(hourIndex: number, nextSelectedCountry = selectedCountry) {
      view.rerender(renderCanvas(hourIndex, nextSelectedCountry));
    },
    rerenderView(viewRequest: GlobeViewRequest) {
      view.rerender(renderCanvas(0, selectedCountry, viewRequest));
    },
  };
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
  // jsdom has no 2D context; the land matrix simply is not drawn.
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const transformOf = (label: string) =>
  screen.getByRole('button', { name: label, hidden: true }).getAttribute('transform');

describe('GlobeCanvas', () => {
  it('renders each origin as a glowing mark with an accessible control over a drawn land layer', () => {
    renderGlobe();

    const globe = screen.getByRole('group', {
      name: 'Globe of IP-based request origins and day and night',
    });
    const origin = screen.getByRole('button', { name: 'China request origin' });

    expect(origin).toHaveAttribute('tabindex', '0');
    expect(origin).toHaveAttribute('aria-pressed', 'false');
    expect(origin.querySelector('circle[data-layer="glow"]')?.getAttribute('fill')).toMatch(
      /^url\(#globe.*-glow\)$/,
    );
    expect(origin.querySelector('circle[data-layer="core"]')).toHaveAttribute(
      'fill',
      ORIGIN_COLORS.core,
    );
    // The hit area reaches past the visible core.
    expect(Number(origin.querySelector('circle[data-layer="hit"]')?.getAttribute('r'))).toBe(
      Number(origin.querySelector('circle[data-layer="core"]')?.getAttribute('r')) + 4,
    );
    expect(document.querySelector('canvas')).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByText(/Glow size = requests · drag to turn/)).toBeInTheDocument();
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

  it('shows a selected ring and pressed state for the selected request origin', () => {
    renderGlobe(vi.fn(), 'n', response(), 'CHN');

    const origin = screen.getByRole('button', { name: 'China request origin' });
    expect(origin).toHaveAttribute('aria-pressed', 'true');
    expect(origin.querySelector('circle[data-layer="selection-ring"]')).not.toHaveAttribute(
      'display',
    );
  });

  it('suppresses the pointer focus outline and keeps a keyboard-only focus ring', () => {
    const data = response();
    data.hours[0].b.push(['USA', 'NA', 4, 40]);
    atlas.coordinates.set('USA', [-98, 39]);
    const { rerenderAt } = renderGlobe(vi.fn(), 'n', data, null, 'window');
    const heat = document.querySelector('g[data-layer="heat"]')!;
    const ringsOf = (label: string) =>
      screen
        .getByRole('button', { name: label, hidden: true })
        .querySelectorAll('circle[data-layer="focus-ring"]');

    const china = screen.getByRole('button', { name: 'China request origin' });
    // Chromium's UA sheet outlines focused SVG on plain :focus, so a click or
    // tap would draw a rectangle; the bubble opts out of that outline.
    expect(china).toHaveStyle({ outline: 'none' });
    const rings = ringsOf('China request origin');
    expect(rings).toHaveLength(2);
    for (const ring of rings) {
      expect(ring.getAttribute('class')?.split(' ')).toEqual([
        'hidden',
        '[g:focus-visible>&]:inline',
      ]);
    }
    const ringRadius = Number(rings[1].getAttribute('r'));
    expect(ringRadius).toBeGreaterThan(
      Number(china.querySelector('circle[data-layer="core"]')?.getAttribute('r')),
    );

    // A selection re-renders the layer; the rings must survive the data join
    // and no ring element may be mistaken for an origin.
    rerenderAt(0, 'CHN');
    expect(heat.children).toHaveLength(2);
    expect(ringsOf('China request origin')).toHaveLength(2);
    expect(ringsOf('United States request origin')).toHaveLength(2);
    expect(Number(ringsOf('China request origin')[1].getAttribute('r'))).toBeCloseTo(
      Number(
        screen
          .getByRole('button', { name: 'China request origin' })
          .querySelector('circle[data-layer="selection-ring"]')
          ?.getAttribute('r'),
      ) + 4,
    );
    atlas.coordinates.delete('USA');
  });

  it('labels the selection, paints it white and on top, and dims the rest', () => {
    const data = response();
    // Close enough to China to share the visible side of the globe.
    data.hours[0].b.push(['JPN', 'AS', 4, 40]);
    atlas.coordinates.set('JPN', [138, 37]);
    renderGlobe(vi.fn(), 'n', data, 'JPN', 'window');

    const globe = screen.getByRole('group', { name: 'Globe of IP-based request origins' });
    const labels = globe.querySelector('g[data-layer="labels"]')!;
    expect(labels).toHaveAttribute('aria-hidden', 'true');
    // The test atlas has no shape for Japan, so its name falls back to the code.
    expect(labels.querySelector('text[data-country="JPN"]')).toHaveTextContent('JPN4');
    expect(labels.querySelector('text[data-country="CHN"]')).toHaveTextContent('China12');
    // The selection keeps a full-strength name; the others fade with their marks.
    expect(labels.querySelector('text[data-country="JPN"] tspan')).toHaveAttribute(
      'fill',
      '#f1f5f9',
    );
    expect(labels.querySelector('text[data-country="CHN"] tspan')).toHaveAttribute(
      'fill',
      'rgba(241,245,249,0.5)',
    );

    const japan = screen.getByRole('button', { name: 'JPN request origin' });
    expect(japan.querySelector('circle[data-layer="core"]')).toHaveAttribute(
      'fill',
      ORIGIN_COLORS.selected,
    );
    expect(japan.querySelector('circle[data-layer="core"]')).toHaveAttribute('opacity', '1');
    const china = screen.getByRole('button', { name: 'China request origin' });
    // Only the marks dim: a keyboard focus ring on a dimmed mark stays at full strength.
    expect(china).not.toHaveAttribute('opacity');
    expect(china.querySelector('circle[data-layer="glow"]')).toHaveAttribute('opacity', '0.35');
    expect(china.querySelector('circle[data-layer="focus-ring"]')).not.toHaveAttribute('opacity');
    // The selection is painted last, though it is the smaller mark.
    const heat = globe.querySelector('g[data-layer="heat"]')!;
    expect(heat.lastElementChild).toBe(japan);
    atlas.coordinates.delete('JPN');
  });

  it('hides marks and labels near the rim, where they would be unreadable', () => {
    const data = response();
    // 85° from the view center at [104, 35]: technically on the front side.
    data.hours[0].b.push(['RIM', 'AS', 4, 40]);
    atlas.coordinates.set('RIM', [104, -50]);
    renderGlobe(vi.fn(), 'n', data, null, 'window');

    expect(
      screen.getByRole('button', { name: 'RIM request origin', hidden: true }),
    ).toHaveAttribute('display', 'none');
    expect(document.querySelector('g[data-layer="labels"] text[data-country="RIM"]')).toBeNull();
    expect(screen.getByRole('button', { name: 'China request origin' })).not.toHaveAttribute(
      'display',
    );
    atlas.coordinates.delete('RIM');
  });

  it('keeps the view when the requested country is already comfortably in sight', () => {
    const { rerenderView } = renderGlobe(vi.fn(), 'n', response(), null, 'window');
    const centered = transformOf('China request origin');

    rerenderView({ id: 2, rotation: [0, 0], unlessVisible: [104, 35] });
    expect(transformOf('China request origin')).toBe(centered);

    rerenderView({ id: 3, rotation: [0, 0], unlessVisible: [0, 0] });
    expect(transformOf('China request origin')).not.toBe(centered);
  });

  it('keeps the selected marker without a false demand bubble in an empty hour', () => {
    const data = response();
    data.hours_index.push('2026-07-16T01:00:00Z');
    data.hours.push({ b: [] });
    const { rerenderAt } = renderGlobe(vi.fn(), 'n', data, 'CHN');

    rerenderAt(1);

    const origin = screen.getByRole('button', { name: 'China request origin' });
    const circles = origin.querySelectorAll('circle');
    expect(origin).toHaveAttribute('aria-pressed', 'true');
    expect(circles[0]).toHaveAttribute('display', 'none');
    expect(circles[1]).toHaveAttribute('display', 'none');
    expect(origin.querySelector('circle[data-layer="selection-ring"]')).not.toHaveAttribute(
      'display',
    );
    expect(origin.querySelector('circle[data-layer="selection-ring"]')).toHaveAttribute('r', '9');
    expect(origin.querySelector('title')).toHaveTextContent(
      'China: no positive requests this hour · 0 requests',
    );
  });

  it('keeps one range-wide radius scale across hours and clamps above p99', () => {
    const data = response();
    data.hours_index = Array.from({ length: 101 }, (_, index) =>
      new Date(Date.UTC(2026, 6, 1, index)).toISOString(),
    );
    data.hours = Array.from({ length: 101 }, (_, index) => ({
      b: [['CHN', 'AS', index < 2 ? 5 : index === 100 ? 10_000 : 10, 0]],
    }));
    const { metricModel, rerenderAt } = renderGlobe(vi.fn(), 'n', data);
    const radiusAt = () =>
      Number(
        screen
          .getByRole('button', { name: 'China request origin' })
          .querySelector('circle[data-layer="core"]')
          ?.getAttribute('r'),
      );

    expect(metricModel.countryHourP99).toBe(10);
    const belowCapRadius = radiusAt();
    rerenderAt(1);
    expect(radiusAt()).toBe(belowCapRadius);
    rerenderAt(2);
    const capRadius = radiusAt();
    expect(belowCapRadius).toBeLessThan(capRadius);
    expect(capRadius).toBe(14);
    rerenderAt(100);
    expect(radiusAt()).toBe(capRadius);
    expect(screen.queryByText(/p99/)).not.toBeInTheDocument();
  });

  it('shades day and night only for an hour, not for a window total', () => {
    const hour = renderGlobe();
    const hourGlobe = screen.getByRole('group', {
      name: 'Globe of IP-based request origins and day and night',
    });
    for (const layer of ['day', 'night', 'terminator']) {
      const path = hourGlobe.querySelector(`path[data-layer="${layer}"]`);
      expect(path).not.toHaveAttribute('display');
      expect(path?.getAttribute('d')).toBeTruthy();
    }
    expect(screen.getByText(/lit land is in daylight/)).toBeInTheDocument();
    hour.unmount();

    renderGlobe(vi.fn(), 'n', response(), null, 'window');
    const windowGlobe = screen.getByRole('group', { name: 'Globe of IP-based request origins' });
    for (const layer of ['day', 'night', 'terminator']) {
      const path = windowGlobe.querySelector(`path[data-layer="${layer}"]`);
      expect(path).toHaveAttribute('display', 'none');
      expect(path).not.toHaveAttribute('d');
    }
    expect(screen.queryByText(/daylight/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'China request origin' })).toBeInTheDocument();
  });

  it('plots the full window aggregation when the overview is active', () => {
    const data = response();
    data.hours_index.push('2026-07-16T01:00:00Z');
    data.hours.push({ b: [['USA', 'NA', 7, 70]] });
    data.meta.rows_total = 19;
    atlas.coordinates.set('USA', [-98, 39]);

    renderGlobe(vi.fn(), 'n', data, null, 'window');

    expect(screen.getByRole('button', { name: 'China request origin' })).toHaveTextContent(
      'China: 12 requests in selected window · 12 requests',
    );
    expect(
      screen.getByRole('button', { name: 'United States request origin', hidden: true }),
    ).toHaveTextContent('United States: 7 requests in selected window · 7 requests');
  });
});
