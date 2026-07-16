'use client';

import {
  geoCentroid,
  geoCircle,
  geoDistance,
  geoGraticule10,
  geoOrthographic,
  geoPath,
  max,
  scaleSqrt,
  select,
} from 'd3';
import type { GeoPermissibleObjects } from 'd3';
import type { Feature, Geometry } from 'geojson';
import { useEffect, useRef } from 'react';
import { feature } from 'topojson-client';
import type { GeometryCollection, Topology } from 'topojson-specification';
import type { GeoAnalyticsResponse } from '@/lib/api/admin';
import { buildColumnIndex, CONTINENT_COLORS, type GeoMetric } from './geoMath';

interface AtlasProperties {
  id?: string;
  name?: string;
  name_long?: string;
}

type AtlasTopology = Topology<{ features: GeometryCollection<AtlasProperties> }>;
type CountryFeature = Feature<Geometry, AtlasProperties>;

export interface PreparedAtlas {
  countries: CountryFeature[];
  byId: Map<string, CountryFeature>;
  coordinates: Map<string, [number, number]>;
}

export type GlobeSelection = { type: 'country'; country: string };

export interface GlobeViewRequest {
  id: number;
  rotation: [number, number];
}

const COORDINATE_OVERRIDES: Readonly<Record<string, [number, number]>> = {
  SGP: [103.82, 1.35],
  HKG: [114.17, 22.3],
  MAC: [113.55, 22.2],
  BHR: [50.55, 26.05],
  MLT: [14.44, 35.9],
  MUS: [57.55, -20.3],
  MDV: [73.5, 4.2],
  AND: [1.52, 42.55],
  LIE: [9.55, 47.15],
  MCO: [7.42, 43.73],
  SMR: [12.45, 43.94],
  BRB: [-59.55, 13.18],
};

const NAME_OVERRIDES: Readonly<Record<string, string>> = {
  SGP: 'Singapore',
  HKG: 'Hong Kong',
  MAC: 'Macao',
  BHR: 'Bahrain',
  MLT: 'Malta',
  MUS: 'Mauritius',
  MDV: 'Maldives',
  AND: 'Andorra',
  LIE: 'Liechtenstein',
  MCO: 'Monaco',
  SMR: 'San Marino',
  BRB: 'Barbados',
  USA: 'United States',
};

const DRAG_THRESHOLD_PX = 6;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object';
}

export function prepareAtlas(raw: unknown): PreparedAtlas {
  if (!isRecord(raw) || raw.type !== 'Topology' || !isRecord(raw.objects)) {
    throw new Error('The bundled world atlas is invalid');
  }
  const topology = raw as unknown as AtlasTopology;
  if (!topology.objects.features) throw new Error('The bundled world atlas has no countries');
  const collection = feature<AtlasProperties>(topology, topology.objects.features);
  if (collection.type !== 'FeatureCollection') {
    throw new Error('The bundled world atlas has an unexpected shape');
  }
  const countries = collection.features as CountryFeature[];
  const byId = new Map<string, CountryFeature>();
  const coordinates = new Map<string, [number, number]>();
  for (const country of countries) {
    const id = country.properties?.id;
    if (!id) continue;
    byId.set(id, country);
    const centroid = geoCentroid(country);
    if (centroid.every(Number.isFinite)) coordinates.set(id, centroid as [number, number]);
  }
  for (const [id, coordinate] of Object.entries(COORDINATE_OVERRIDES)) {
    coordinates.set(id, coordinate);
  }
  return { countries, byId, coordinates };
}

export function atlasCountryName(atlas: PreparedAtlas, alpha3: string): string {
  return (
    NAME_OVERRIDES[alpha3] ??
    atlas.byId.get(alpha3)?.properties?.name ??
    atlas.byId.get(alpha3)?.properties?.name_long ??
    alpha3
  );
}

function metricLabel(metric: GeoMetric): string {
  if (metric === 'tout') return 'output tokens';
  return 'requests';
}

function positiveNumber(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, value) : 0;
}

function keyboardSelect(event: KeyboardEvent, action: () => void): void {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    action();
  }
}

function dayNightGeometry(date: Date) {
  const startOfYear = Date.UTC(date.getUTCFullYear(), 0, 0);
  const dayOfYear = Math.floor((date.getTime() - startOfYear) / 86_400_000);
  const declination = 23.44 * Math.sin((2 * Math.PI * (284 + dayOfYear)) / 365);
  const utcHour = date.getUTCHours() + date.getUTCMinutes() / 60;
  const subsolarLongitude = ((180 - utcHour * 15 + 540) % 360) - 180;
  const antipodeLongitude = ((subsolarLongitude + 360) % 360) - 180;
  return {
    day: geoCircle().center([subsolarLongitude, declination]).radius(90)(),
    night: geoCircle().center([antipodeLongitude, -declination]).radius(90)(),
  };
}

interface CountryDot {
  country: string;
  continent: string;
  value: number;
  requests: number;
  coordinate: [number, number];
}

export function GlobeCanvas({
  data,
  atlas,
  hourIndex,
  metric,
  viewRequest,
  onSelect,
}: {
  data: GeoAnalyticsResponse;
  atlas: PreparedAtlas;
  hourIndex: number;
  metric: GeoMetric;
  viewRequest: GlobeViewRequest;
  onSelect: (selection: GlobeSelection) => void;
}) {
  const stageRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const projectionRef = useRef(
    geoOrthographic().clipAngle(90).precision(0.4).rotate([-100, -28, 0]),
  );
  const staticRedrawRef = useRef<() => void>(() => undefined);
  const dynamicRedrawRef = useRef<() => void>(() => undefined);

  useEffect(() => {
    const stageElement = stageRef.current;
    const svgElement = svgRef.current;
    if (!stageElement || !svgElement) return;

    const projection = projectionRef.current;
    const path = geoPath(projection);
    const svg = select(svgElement);
    svg.selectAll('*').remove();

    const sphere = svg
      .append('path')
      .attr('data-layer', 'sphere')
      .datum({ type: 'Sphere' } as GeoPermissibleObjects)
      .attr('fill', 'rgba(16,21,32,0.96)')
      .attr('stroke', 'rgba(255,255,255,0.12)');
    const graticule = svg
      .append('path')
      .attr('data-layer', 'graticule')
      .datum(geoGraticule10())
      .attr('fill', 'none')
      .attr('stroke', 'rgba(255,255,255,0.055)')
      .attr('stroke-width', 0.6);
    const countriesLayer = svg.append('g').attr('data-layer', 'countries');
    const countryPaths = countriesLayer
      .selectAll<SVGPathElement, CountryFeature>('path')
      .data(atlas.countries)
      .join('path')
      .attr('fill', '#1f2836')
      .attr('stroke', 'rgba(255,255,255,0.06)')
      .attr('stroke-width', 0.5);
    svg.append('path').attr('data-layer', 'day').attr('fill', 'rgba(255,244,214,0.08)');
    svg.append('path').attr('data-layer', 'night').attr('fill', 'rgba(2,4,12,0.48)');
    svg
      .append('path')
      .attr('data-layer', 'terminator')
      .attr('fill', 'none')
      .attr('stroke', 'rgba(205,220,255,0.34)')
      .attr('stroke-width', 1.1)
      .attr('stroke-dasharray', '5 4');
    svg.append('g').attr('data-layer', 'heat');

    const redrawStatic = () => {
      sphere.attr('d', path);
      graticule.attr('d', path);
      countryPaths.attr('d', path);
    };
    staticRedrawRef.current = redrawStatic;

    const resize = () => {
      const bounds = stageElement.getBoundingClientRect();
      const width = Math.max(320, bounds.width || 800);
      const height = Math.max(360, bounds.height || 520);
      svg.attr('viewBox', `0 0 ${width} ${height}`);
      projection.translate([width * 0.5, height * 0.5]).scale(Math.min(width, height) * 0.46);
      redrawStatic();
      dynamicRedrawRef.current();
    };
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(resize);
    observer?.observe(stageElement);
    resize();

    let drag:
      | {
          pointerId: number;
          start: [number, number];
          rotation: [number, number, number];
          active: boolean;
        }
      | undefined;
    let suppressClick = false;
    let suppressClickReset: ReturnType<typeof setTimeout> | undefined;
    const pointerDown = (event: PointerEvent) => {
      if (event.button !== 0) return;
      if (suppressClickReset !== undefined) {
        clearTimeout(suppressClickReset);
        suppressClickReset = undefined;
      }
      drag = {
        pointerId: event.pointerId,
        start: [event.clientX, event.clientY],
        rotation: projection.rotate(),
        active: false,
      };
      suppressClick = false;
    };
    const pointerMove = (event: PointerEvent) => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      const deltaX = event.clientX - drag.start[0];
      const deltaY = event.clientY - drag.start[1];
      if (!drag.active && Math.hypot(deltaX, deltaY) < DRAG_THRESHOLD_PX) return;
      if (!drag.active) {
        drag.active = true;
        suppressClick = true;
        try {
          svgElement.setPointerCapture(event.pointerId);
        } catch {
          // Rotation can continue while the pointer stays over the globe.
        }
      }
      projection.rotate([
        drag.rotation[0] + deltaX * 0.28,
        Math.max(-75, Math.min(75, drag.rotation[1] - deltaY * 0.22)),
        0,
      ]);
      redrawStatic();
      dynamicRedrawRef.current();
    };
    const pointerEnd = (event: PointerEvent) => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      const wasDragging = drag.active;
      drag = undefined;
      try {
        if (svgElement.hasPointerCapture(event.pointerId)) {
          svgElement.releasePointerCapture(event.pointerId);
        }
      } catch {
        // Pointer capture may be unavailable or already released.
      }
      if (wasDragging) {
        suppressClick = true;
        suppressClickReset = setTimeout(() => {
          suppressClick = false;
          suppressClickReset = undefined;
        }, 0);
      }
    };
    const clickCapture = (event: MouseEvent) => {
      if (!suppressClick) return;
      event.preventDefault();
      event.stopPropagation();
      suppressClick = false;
      if (suppressClickReset !== undefined) {
        clearTimeout(suppressClickReset);
        suppressClickReset = undefined;
      }
    };
    svgElement.addEventListener('pointerdown', pointerDown);
    svgElement.addEventListener('pointermove', pointerMove);
    window.addEventListener('pointerup', pointerEnd);
    window.addEventListener('pointercancel', pointerEnd);
    svgElement.addEventListener('click', clickCapture, true);

    return () => {
      observer?.disconnect();
      svgElement.removeEventListener('pointerdown', pointerDown);
      svgElement.removeEventListener('pointermove', pointerMove);
      window.removeEventListener('pointerup', pointerEnd);
      window.removeEventListener('pointercancel', pointerEnd);
      svgElement.removeEventListener('click', clickCapture, true);
      if (suppressClickReset !== undefined) clearTimeout(suppressClickReset);
      staticRedrawRef.current = () => undefined;
      svg.selectAll('*').remove();
    };
  }, [atlas]);

  useEffect(() => {
    const svgElement = svgRef.current;
    if (!svgElement) return;

    const projection = projectionRef.current;
    const path = geoPath(projection);
    const svg = select(svgElement);
    const daySide = svg.select<SVGPathElement>('path[data-layer="day"]');
    const nightSide = svg.select<SVGPathElement>('path[data-layer="night"]');
    const terminator = svg.select<SVGPathElement>('path[data-layer="terminator"]');
    const heatLayer = svg.select<SVGGElement>('g[data-layer="heat"]');

    const bucketIndex = buildColumnIndex(data.bucket_cols);
    const countryPosition = bucketIndex.c;
    const continentPosition = bucketIndex.cont;
    const requestPosition = bucketIndex.n;
    const metricPosition = bucketIndex[metric];
    const countryValues = new Map<string, Omit<CountryDot, 'coordinate'>>();
    for (const row of data.hours[hourIndex]?.b ?? []) {
      const country = String(row[countryPosition] ?? '');
      const existing = countryValues.get(country) ?? {
        country,
        continent: String(row[continentPosition] ?? '?'),
        value: 0,
        requests: 0,
      };
      existing.value += positiveNumber(row[metricPosition]);
      existing.requests += positiveNumber(row[requestPosition]);
      countryValues.set(country, existing);
    }
    const dots: CountryDot[] = [...countryValues.values()]
      .map((dot) => {
        const coordinate = atlas.coordinates.get(dot.country);
        return coordinate ? { ...dot, coordinate } : null;
      })
      .filter(
        (dot): dot is CountryDot => dot !== null && !dot.country.startsWith('?') && dot.value > 0,
      );
    const radius = scaleSqrt()
      .domain([0, Math.max(1, max(dots, (dot) => dot.value) ?? 0)])
      .range([2.5, 26]);

    const heat = heatLayer
      .selectAll<SVGGElement, CountryDot>('g')
      .data(dots, (dot) => dot.country)
      .join((enter) => {
        const group = enter.append('g').attr('role', 'button').attr('tabindex', 0);
        group.append('circle').attr('opacity', 0.16);
        group.append('circle').attr('stroke', 'rgba(11,14,20,0.9)').attr('stroke-width', 1);
        group.append('title');
        return group;
      })
      .attr('aria-label', (dot) => `${atlasCountryName(atlas, dot.country)} request origin`)
      .on('click', (_, dot) => onSelect({ type: 'country', country: dot.country }))
      .on('keydown', (event, dot) =>
        keyboardSelect(event, () => onSelect({ type: 'country', country: dot.country })),
      );
    heat
      .select('circle:first-of-type')
      .attr('fill', (dot) => CONTINENT_COLORS[dot.continent] ?? CONTINENT_COLORS['?'])
      .attr('r', (dot) => radius(dot.value));
    heat
      .select('circle:nth-of-type(2)')
      .attr('fill', (dot) => CONTINENT_COLORS[dot.continent] ?? CONTINENT_COLORS['?'])
      .attr('r', (dot) => Math.max(2, radius(dot.value) * 0.22));
    heat
      .select('title')
      .text(
        (dot) =>
          `${atlasCountryName(atlas, dot.country)}: ${Math.round(dot.value).toLocaleString()} ${metricLabel(metric)} · ${dot.requests.toLocaleString()} requests`,
      );

    const selectedDate = new Date(data.hours_index[hourIndex]);
    const { day, night } = dayNightGeometry(
      Number.isNaN(selectedDate.getTime()) ? new Date(0) : selectedDate,
    );
    daySide.datum(day);
    nightSide.datum(night);
    terminator.datum(day);

    const isFront = (coordinate: [number, number]) => {
      const rotation = projection.rotate();
      return geoDistance([-rotation[0], -rotation[1]], coordinate) < Math.PI / 2;
    };
    const redrawDynamic = () => {
      daySide.attr('d', path(day));
      nightSide.attr('d', path(night));
      terminator.attr('d', path(day));
      heat
        .attr('transform', (dot) => {
          const point = projection(dot.coordinate);
          return point ? `translate(${point[0]},${point[1]})` : 'translate(-999,-999)';
        })
        .attr('display', (dot) => (isFront(dot.coordinate) ? null : 'none'));
    };
    dynamicRedrawRef.current = redrawDynamic;
    redrawDynamic();

    return () => {
      if (dynamicRedrawRef.current === redrawDynamic) {
        dynamicRedrawRef.current = () => undefined;
      }
    };
  }, [atlas, data, hourIndex, metric, onSelect]);

  useEffect(() => {
    projectionRef.current.rotate([viewRequest.rotation[0], viewRequest.rotation[1], 0]);
    staticRedrawRef.current();
    dynamicRedrawRef.current();
  }, [viewRequest]);

  return (
    <div
      ref={stageRef}
      className="relative h-[clamp(380px,52vh,620px)] min-w-0 overflow-hidden rounded-xl border border-white/10 bg-[#0b0e14]"
    >
      <svg
        ref={svgRef}
        aria-label="Globe of IP-based request origins and day and night"
        className="block h-full w-full cursor-grab touch-none active:cursor-grabbing"
        role="group"
      />
      <div className="pointer-events-none absolute bottom-3 left-3 flex flex-wrap gap-3 text-[11px] text-gray-400">
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-full" style={{ background: CONTINENT_COLORS.AS }} />{' '}
          request origin · color = continent
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-[#cddcff]/40" /> day/night
        </span>
      </div>
      <p className="pointer-events-none absolute bottom-3 right-3 hidden text-[11px] text-gray-500 sm:block">
        Drag to rotate · select a request origin
      </p>
    </div>
  );
}
