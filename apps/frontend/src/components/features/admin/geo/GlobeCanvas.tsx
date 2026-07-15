'use client';

import * as d3 from 'd3';
import type { Feature, Geometry } from 'geojson';
import { useEffect, useRef } from 'react';
import { feature } from 'topojson-client';
import type { GeometryCollection, Topology } from 'topojson-specification';
import type { GeoAnalyticsResponse, GeoProvider } from '@/lib/api/admin';
import {
  aggregateFlowsByCountryProvider,
  buildColumnIndex,
  buildCountryContinentMap,
  CONTINENT_COLORS,
  type GeoMetric,
} from './geoMath';

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

export type GlobeSelection =
  | { type: 'country'; country: string }
  | { type: 'provider'; providerId: string }
  | { type: 'flow'; country: string; providerId: string };

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
    const centroid = d3.geoCentroid(country);
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
  if (metric === 'gs') return 'compute seconds';
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
    day: d3.geoCircle().center([subsolarLongitude, declination]).radius(90)(),
    night: d3.geoCircle().center([antipodeLongitude, -declination]).radius(90)(),
  };
}

function greatCircle(from: [number, number], to: [number, number]): GeoJSON.LineString {
  const interpolate = d3.geoInterpolate(from, to);
  return {
    type: 'LineString',
    coordinates: d3.range(31).map((index) => interpolate(index / 30)),
  };
}

interface CountryDot {
  country: string;
  continent: string;
  value: number;
  requests: number;
  users: number;
  coordinate: [number, number];
}

interface GlobeFlow {
  country: string;
  provider: GeoProvider;
  requests: number;
  coordinate: [number, number];
}

export function GlobeCanvas({
  data,
  atlas,
  hourIndex,
  metric,
  animateFlows,
  viewRequest,
  onSelect,
}: {
  data: GeoAnalyticsResponse;
  atlas: PreparedAtlas;
  hourIndex: number;
  metric: GeoMetric;
  animateFlows: boolean;
  viewRequest: GlobeViewRequest;
  onSelect: (selection: GlobeSelection) => void;
}) {
  const stageRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const projectionRef = useRef(
    d3.geoOrthographic().clipAngle(90).precision(0.4).rotate([-100, -28, 0]),
  );
  const redrawRef = useRef<() => void>(() => undefined);

  useEffect(() => {
    const stageElement = stageRef.current;
    const svgElement = svgRef.current;
    if (!stageElement || !svgElement) return;

    const projection = projectionRef.current;
    const path = d3.geoPath(projection);
    const svg = d3.select(svgElement);
    svg.selectAll('*').remove();

    const sphere = svg
      .append('path')
      .datum({ type: 'Sphere' } as d3.GeoPermissibleObjects)
      .attr('fill', 'rgba(16,21,32,0.96)')
      .attr('stroke', 'rgba(255,255,255,0.12)');
    const graticule = svg
      .append('path')
      .datum(d3.geoGraticule10())
      .attr('fill', 'none')
      .attr('stroke', 'rgba(255,255,255,0.055)')
      .attr('stroke-width', 0.6);
    const countriesLayer = svg.append('g');
    const countryPaths = countriesLayer
      .selectAll<SVGPathElement, CountryFeature>('path')
      .data(atlas.countries)
      .join('path')
      .attr('fill', '#1f2836')
      .attr('stroke', 'rgba(255,255,255,0.06)')
      .attr('stroke-width', 0.5);
    const daySide = svg.append('path').attr('fill', 'rgba(255,244,214,0.08)');
    const nightSide = svg.append('path').attr('fill', 'rgba(2,4,12,0.48)');
    const terminator = svg
      .append('path')
      .attr('fill', 'none')
      .attr('stroke', 'rgba(205,220,255,0.34)')
      .attr('stroke-width', 1.1)
      .attr('stroke-dasharray', '5 4');
    const arcsLayer = svg.append('g');
    const heatLayer = svg.append('g');
    const providersLayer = svg.append('g');

    const bucketIndex = buildColumnIndex(data.bucket_cols);
    const countryPosition = bucketIndex.c;
    const continentPosition = bucketIndex.cont;
    const requestPosition = bucketIndex.n;
    const usersPosition = bucketIndex.users;
    const metricPosition = bucketIndex[metric];
    const countryValues = new Map<string, Omit<CountryDot, 'coordinate'>>();
    for (const row of data.hours[hourIndex]?.b ?? []) {
      const country = String(row[countryPosition] ?? '');
      const existing = countryValues.get(country) ?? {
        country,
        continent: String(row[continentPosition] ?? '?'),
        value: 0,
        requests: 0,
        users: 0,
      };
      existing.value += positiveNumber(row[metricPosition]);
      existing.requests += positiveNumber(row[requestPosition]);
      existing.users += positiveNumber(row[usersPosition]);
      countryValues.set(country, existing);
    }
    const dots: CountryDot[] = [...countryValues.values()]
      .map((dot) => {
        const coordinate = atlas.coordinates.get(dot.country);
        return coordinate ? { ...dot, coordinate } : null;
      })
      .filter((dot): dot is CountryDot => dot !== null && !dot.country.startsWith('?'));
    const radius = d3
      .scaleSqrt()
      .domain([0, d3.max(dots, (dot) => dot.value) ?? 1])
      .range([2.5, 26]);

    const providersById = new Map(data.providers.map((provider) => [provider.id, provider]));
    const localProviders = data.providers.filter(
      (provider): provider is GeoProvider & { coord: [number, number] } =>
        provider.kind === 'local' && provider.coord !== null,
    );
    const aggregatedFlows = aggregateFlowsByCountryProvider(data, hourIndex);
    const flows: GlobeFlow[] = aggregatedFlows
      .map((flow) => {
        const provider = providersById.get(flow.providerId);
        const coordinate = atlas.coordinates.get(flow.country);
        if (!provider || provider.kind !== 'local' || !provider.coord || !coordinate) return null;
        return { country: flow.country, provider, requests: flow.requests, coordinate };
      })
      .filter((flow): flow is GlobeFlow => flow !== null)
      .sort((a, b) => b.requests - a.requests)
      .slice(0, 14);
    const flowWidth = d3
      .scaleSqrt()
      .domain([0, d3.max(flows, (flow) => flow.requests) ?? 1])
      .range([0.6, 4]);
    const countryContinents = buildCountryContinentMap(data);

    const heat = heatLayer
      .selectAll<SVGGElement, CountryDot>('g')
      .data(dots, (dot) => dot.country)
      .join((enter) => {
        const group = enter.append('g').attr('role', 'button').attr('tabindex', 0);
        group.append('circle').attr('fill', '#d95926').attr('opacity', 0.16);
        group
          .append('circle')
          .attr('fill', '#d95926')
          .attr('stroke', 'rgba(11,14,20,0.9)')
          .attr('stroke-width', 1);
        group.append('title');
        return group;
      })
      .attr('aria-label', (dot) => `${atlasCountryName(atlas, dot.country)} request origin`)
      .on('click', (_, dot) => onSelect({ type: 'country', country: dot.country }))
      .on('keydown', (event, dot) =>
        keyboardSelect(event, () => onSelect({ type: 'country', country: dot.country })),
      );
    heat.select('circle:first-of-type').attr('r', (dot) => radius(dot.value));
    heat.select('circle:nth-of-type(2)').attr('r', (dot) => Math.max(2, radius(dot.value) * 0.22));
    heat
      .select('title')
      .text(
        (dot) =>
          `${atlasCountryName(atlas, dot.country)}: ${Math.round(dot.value).toLocaleString()} ${metricLabel(metric)} · ${dot.requests.toLocaleString()} requests`,
      );

    const arcs = arcsLayer
      .selectAll<SVGPathElement, GlobeFlow>('path')
      .data(flows, (flow) => `${flow.country}>${flow.provider.id}`)
      .join('path')
      .attr('class', 'geo-demand-flow')
      .attr('fill', 'none')
      .attr('stroke', (flow) => CONTINENT_COLORS[countryContinents.get(flow.country) ?? '?'])
      .attr('stroke-width', (flow) => flowWidth(flow.requests))
      .attr('stroke-linecap', 'round')
      .attr('stroke-dasharray', '4 9')
      .attr('opacity', 0.82)
      .attr('role', 'button')
      .attr('tabindex', 0)
      .attr(
        'aria-label',
        (flow) =>
          `${atlasCountryName(atlas, flow.country)} to ${flow.provider.label}: ${flow.requests} requests`,
      )
      .style('animation', animateFlows ? 'geo-demand-flow 2.2s linear infinite' : 'none')
      .on('click', (_, flow) =>
        onSelect({ type: 'flow', country: flow.country, providerId: flow.provider.id }),
      )
      .on('keydown', (event, flow) =>
        keyboardSelect(event, () =>
          onSelect({ type: 'flow', country: flow.country, providerId: flow.provider.id }),
        ),
      );
    arcs
      .selectAll('title')
      .data((flow) => [flow])
      .join('title')
      .text(
        (flow) =>
          `${atlasCountryName(atlas, flow.country)} → ${flow.provider.label}: ${flow.requests.toLocaleString()} requests`,
      );

    const inbound = new Map(localProviders.map((provider) => [provider.id, 0]));
    for (const flow of aggregatedFlows) {
      if (inbound.has(flow.providerId)) {
        inbound.set(flow.providerId, (inbound.get(flow.providerId) ?? 0) + flow.requests);
      }
    }
    const maximumInbound = Math.max(1, ...inbound.values());
    const providers = providersLayer
      .selectAll<SVGGElement, GeoProvider & { coord: [number, number] }>('g')
      .data(localProviders, (provider) => provider.id)
      .join((enter) => {
        const group = enter.append('g').attr('role', 'button').attr('tabindex', 0);
        group.append('circle').attr('fill', '#3987e5').attr('opacity', 0.16);
        group
          .append('circle')
          .attr('fill', '#3987e5')
          .attr('stroke', '#0b0e14')
          .attr('stroke-width', 2);
        group
          .append('text')
          .attr('x', 11)
          .attr('y', -8)
          .attr('fill', '#f2f4f8')
          .attr('font-size', 11)
          .attr('font-weight', 600)
          .attr('paint-order', 'stroke')
          .attr('stroke', '#0b0e14')
          .attr('stroke-width', 3)
          .attr('stroke-linejoin', 'round');
        group.append('title');
        return group;
      })
      .attr('aria-label', (provider) => `${provider.label} local provider`)
      .on('click', (_, provider) => onSelect({ type: 'provider', providerId: provider.id }))
      .on('keydown', (event, provider) =>
        keyboardSelect(event, () => onSelect({ type: 'provider', providerId: provider.id })),
      );
    providers
      .select('circle:first-of-type')
      .attr(
        'r',
        (provider) => 9 + 16 * Math.sqrt((inbound.get(provider.id) ?? 0) / maximumInbound),
      );
    providers.select('circle:nth-of-type(2)').attr('r', 4.5);
    providers.select('text').text((provider) => provider.label);
    providers
      .select('title')
      .text(
        (provider) =>
          `${provider.label} (${provider.region ?? 'unknown region'}): ${(inbound.get(provider.id) ?? 0).toLocaleString()} requests inbound`,
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
      return d3.geoDistance([-rotation[0], -rotation[1]], coordinate) < Math.PI / 2;
    };
    const redraw = () => {
      sphere.attr('d', path);
      graticule.attr('d', path);
      countryPaths.attr('d', path);
      daySide.attr('d', path(day));
      nightSide.attr('d', path(night));
      terminator.attr('d', path(day));
      heat
        .attr('transform', (dot) => {
          const point = projection(dot.coordinate);
          return point ? `translate(${point[0]},${point[1]})` : 'translate(-999,-999)';
        })
        .attr('display', (dot) => (isFront(dot.coordinate) ? null : 'none'));
      arcs.attr('d', (flow) => path(greatCircle(flow.coordinate, flow.provider.coord!)));
      providers
        .attr('transform', (provider) => {
          const point = projection(provider.coord);
          return point ? `translate(${point[0]},${point[1]})` : 'translate(-999,-999)';
        })
        .attr('display', (provider) => (isFront(provider.coord) ? null : 'none'));
    };
    redrawRef.current = redraw;

    const resize = () => {
      const bounds = stageElement.getBoundingClientRect();
      const width = Math.max(320, bounds.width || 800);
      const height = Math.max(360, bounds.height || 520);
      svg.attr('viewBox', `0 0 ${width} ${height}`);
      projection.translate([width * 0.5, height * 0.5]).scale(Math.min(width, height) * 0.46);
      redraw();
    };
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(resize);
    observer?.observe(stageElement);
    resize();

    let dragStart: [number, number] | null = null;
    let rotationStart: [number, number, number] | null = null;
    const pointerDown = (event: PointerEvent) => {
      dragStart = [event.clientX, event.clientY];
      rotationStart = projection.rotate();
      svgElement.setPointerCapture?.(event.pointerId);
    };
    const pointerMove = (event: PointerEvent) => {
      if (!dragStart || !rotationStart) return;
      projection.rotate([
        rotationStart[0] + (event.clientX - dragStart[0]) * 0.28,
        Math.max(-75, Math.min(75, rotationStart[1] - (event.clientY - dragStart[1]) * 0.22)),
        0,
      ]);
      redraw();
    };
    const pointerEnd = () => {
      dragStart = null;
      rotationStart = null;
    };
    svgElement.addEventListener('pointerdown', pointerDown);
    svgElement.addEventListener('pointermove', pointerMove);
    svgElement.addEventListener('pointerup', pointerEnd);
    svgElement.addEventListener('pointercancel', pointerEnd);

    return () => {
      observer?.disconnect();
      svgElement.removeEventListener('pointerdown', pointerDown);
      svgElement.removeEventListener('pointermove', pointerMove);
      svgElement.removeEventListener('pointerup', pointerEnd);
      svgElement.removeEventListener('pointercancel', pointerEnd);
      redrawRef.current = () => undefined;
      svg.selectAll('*').remove();
    };
  }, [animateFlows, atlas, data, hourIndex, metric, onSelect]);

  useEffect(() => {
    projectionRef.current.rotate([viewRequest.rotation[0], viewRequest.rotation[1], 0]);
    redrawRef.current();
  }, [viewRequest]);

  return (
    <div
      ref={stageRef}
      className="relative h-[clamp(380px,52vh,620px)] min-w-0 overflow-hidden rounded-xl border border-white/10 bg-[#0b0e14]"
    >
      <style>{`
        @keyframes geo-demand-flow { to { stroke-dashoffset: -26; } }
        @media (prefers-reduced-motion: reduce) { .geo-demand-flow { animation: none !important; } }
      `}</style>
      <svg
        ref={svgRef}
        aria-label="Globe of IP-based request origins, day and night, and flows to local providers"
        className="block h-full w-full cursor-grab touch-none active:cursor-grabbing"
        role="img"
      />
      <div className="pointer-events-none absolute bottom-3 left-3 flex flex-wrap gap-3 text-[11px] text-gray-400">
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-[#d95926]" /> request origin
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-[#3987e5]" /> local provider
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-[#cddcff]/40" /> day/night
        </span>
      </div>
      <p className="pointer-events-none absolute bottom-3 right-3 hidden text-[11px] text-gray-500 sm:block">
        Drag to rotate · select a dot, route, or node
      </p>
    </div>
  );
}
