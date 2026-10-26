'use client';

import {
  geoCentroid,
  geoCircle,
  geoDistance,
  geoOrthographic,
  geoPath,
  scaleSqrt,
  select,
} from 'd3';
import type { Feature, Geometry } from 'geojson';
import { useEffect, useId, useRef } from 'react';
import { feature } from 'topojson-client';
import type { GeometryCollection, Topology } from 'topojson-specification';
import { ORIGIN_COLORS, type GeoMetric, type GeoMetricModel, type OriginSummary } from './geoMath';
import { estimateLabelWidth, placeLabels } from './globeLabels';

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
  animate?: boolean;
  /** Leave the view alone when this point is already comfortably in sight. */
  unlessVisible?: [number, number];
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
const DEGREES = 180 / Math.PI;
/** A mark nearer the rim than this is foreshortened past reading, so it is not drawn. */
const VISIBLE_LIMIT_DEG = 80;
/** A selection within this of the center is already in view: no fly-to. */
const COMFORTABLE_VIEW_DEG = 55;
const MAX_LABELS = 5;

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

function formatCompact(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 10_000) return `${(value / 1_000).toFixed(1)}k`;
  return Math.round(value).toLocaleString('en-US');
}

function keyboardSelect(event: KeyboardEvent, action: () => void): void {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    action();
  }
}

function centerOf(rotation: readonly number[]): [number, number] {
  return [-rotation[0], -rotation[1]];
}

function sunPosition(date: Date) {
  const startOfYear = Date.UTC(date.getUTCFullYear(), 0, 0);
  const dayOfYear = Math.floor((date.getTime() - startOfYear) / 86_400_000);
  const declination = 23.44 * Math.sin((2 * Math.PI * (284 + dayOfYear)) / 365);
  const utcHour = date.getUTCHours() + date.getUTCMinutes() / 60;
  const subsolarLongitude = ((180 - utcHour * 15 + 540) % 360) - 180;
  return [subsolarLongitude, declination] as [number, number];
}

function dayNightGeometry(sun: [number, number]) {
  const antipodeLongitude = ((sun[0] + 360) % 360) - 180;
  return {
    day: geoCircle().center(sun).radius(90)(),
    night: geoCircle().center([antipodeLongitude, -sun[1]]).radius(90)(),
  };
}

interface CountryDot {
  country: string;
  continent: string;
  value: number;
  requests: number;
  coordinate: [number, number];
  selectedOnly: boolean;
}

/**
 * The night-grid globe. A canvas underneath draws the sphere, its halo and the
 * land as a dot matrix — thousands of points, redrawn on every drag frame. An
 * SVG on top holds everything a person interacts with or reads: one focusable
 * button per origin (a glow sized by volume around a bright core), direct
 * labels for the biggest origins, the day/night terminator for an hour, and the
 * drag surface.
 */
export function GlobeCanvas({
  atlas,
  land,
  metricModel,
  mode,
  viewRequest,
  onSelect,
  selectedCountry,
  snapshotTimestamp,
  summary,
}: {
  atlas: PreparedAtlas;
  /** Land as [lon, lat] points (`landPoints`), computed once per atlas. */
  land: readonly [number, number][];
  metricModel: GeoMetricModel;
  mode: 'window' | 'hour';
  viewRequest: GlobeViewRequest;
  onSelect: (selection: GlobeSelection) => void;
  selectedCountry: string | null;
  snapshotTimestamp: string | undefined;
  summary: OriginSummary;
}) {
  const stageRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const gradientId = `globe${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const projectionRef = useRef(
    geoOrthographic().clipAngle(90).precision(0.4).rotate([-100, -28, 0]),
  );
  const sizeRef = useRef({ width: 800, height: 520 });
  // The sun of the selected hour lights its half of the land; null for a total.
  const sunRef = useRef<[number, number] | null>(null);
  const staticRedrawRef = useRef<() => void>(() => undefined);
  const dynamicRedrawRef = useRef<() => void>(() => undefined);

  useEffect(() => {
    const stageElement = stageRef.current;
    const canvasElement = canvasRef.current;
    const svgElement = svgRef.current;
    if (!stageElement || !canvasElement || !svgElement) return;

    const projection = projectionRef.current;
    const svg = select(svgElement);
    svg.selectAll('*').remove();

    const glow = svg.append('defs').append('radialGradient').attr('id', `${gradientId}-glow`);
    glow.append('stop').attr('offset', 0).attr('stop-color', 'rgba(125,211,252,0.5)');
    glow.append('stop').attr('offset', 0.5).attr('stop-color', 'rgba(125,211,252,0.2)');
    glow.append('stop').attr('offset', 1).attr('stop-color', 'rgba(125,211,252,0)');

    svg
      .append('path')
      .attr('data-layer', 'day')
      .attr('fill', 'none')
      .attr('pointer-events', 'none');
    svg
      .append('path')
      .attr('data-layer', 'night')
      .attr('fill', 'rgba(2,6,23,0.3)')
      .attr('pointer-events', 'none');
    svg
      .append('path')
      .attr('data-layer', 'terminator')
      .attr('fill', 'none')
      .attr('pointer-events', 'none')
      .attr('stroke', 'rgba(186,230,253,0.28)')
      .attr('stroke-width', 1)
      .attr('stroke-dasharray', '2 4');
    svg.append('g').attr('data-layer', 'heat');
    svg
      .append('g')
      .attr('data-layer', 'labels')
      .attr('aria-hidden', 'true')
      .attr('pointer-events', 'none')
      .attr('font-size', 12.5)
      .attr('font-weight', 500);

    const drawBase = () => {
      const context = canvasElement.getContext('2d');
      if (!context) return;
      const { width, height } = sizeRef.current;
      const ratio = window.devicePixelRatio || 1;
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, width, height);
      const [cx, cy] = projection.translate();
      const radius = projection.scale();

      const halo = context.createRadialGradient(cx, cy, radius * 0.96, cx, cy, radius * 1.22);
      halo.addColorStop(0, 'rgba(56,189,248,0.16)');
      halo.addColorStop(1, 'rgba(56,189,248,0)');
      context.fillStyle = halo;
      context.beginPath();
      context.arc(cx, cy, radius * 1.22, 0, Math.PI * 2);
      context.fill();

      const body = context.createRadialGradient(
        cx - radius * 0.3,
        cy - radius * 0.35,
        radius * 0.1,
        cx,
        cy,
        radius,
      );
      body.addColorStop(0, '#13213b');
      body.addColorStop(1, '#0a1222');
      context.fillStyle = body;
      context.beginPath();
      context.arc(cx, cy, radius, 0, Math.PI * 2);
      context.fill();

      // Each land point fades and shrinks toward the rim, which is what makes
      // the matrix read as a sphere; in an hour, the sunlit half is brighter.
      const center = centerOf(projection.rotate());
      const sun = sunRef.current;
      for (const point of land) {
        const distance = geoDistance(center, point);
        if (distance >= Math.PI / 2) continue;
        const projected = projection(point);
        if (!projected) continue;
        const facing = Math.cos(distance);
        let alpha = 0.16 + 0.5 * facing;
        let color = '148,163,184';
        if (sun) {
          const daylight = geoDistance(sun, point) < Math.PI / 2;
          alpha *= daylight ? 1.25 : 0.5;
          if (daylight) color = '186,198,214';
        }
        context.fillStyle = `rgba(${color},${Math.min(0.9, alpha)})`;
        context.beginPath();
        context.arc(projected[0], projected[1], 0.55 + 0.75 * facing, 0, Math.PI * 2);
        context.fill();
      }
      context.strokeStyle = 'rgba(125,211,252,0.16)';
      context.lineWidth = 1;
      context.beginPath();
      context.arc(cx, cy, radius, 0, Math.PI * 2);
      context.stroke();
    };
    staticRedrawRef.current = drawBase;

    const resize = () => {
      const bounds = stageElement.getBoundingClientRect();
      const width = Math.max(280, bounds.width || 800);
      const height = Math.max(300, bounds.height || 520);
      sizeRef.current = { width, height };
      const ratio = window.devicePixelRatio || 1;
      canvasElement.width = Math.round(width * ratio);
      canvasElement.height = Math.round(height * ratio);
      svg.attr('viewBox', `0 0 ${width} ${height}`);
      projection
        .translate([width * 0.5, height * 0.5])
        .scale(Math.min(width * 0.44, height * 0.42));
      drawBase();
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
      drawBase();
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
  }, [atlas, gradientId, land]);

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
    const labelLayer = svg.select<SVGGElement>('g[data-layer="labels"]');

    const countryValues = new Map(summary.origins.map((origin) => [origin.country, origin]));
    const dots: CountryDot[] = summary.origins
      .map((dot) => {
        const coordinate = atlas.coordinates.get(dot.country);
        return coordinate ? { ...dot, coordinate, selectedOnly: false } : null;
      })
      .filter(
        (dot): dot is CountryDot => dot !== null && !dot.country.startsWith('?') && dot.value > 0,
      );
    if (selectedCountry && !dots.some((dot) => dot.country === selectedCountry)) {
      const coordinate = atlas.coordinates.get(selectedCountry);
      if (coordinate) {
        const current = countryValues.get(selectedCountry);
        dots.push({
          country: selectedCountry,
          continent: current?.continent ?? '?',
          value: current?.value ?? 0,
          requests: current?.requests ?? 0,
          coordinate,
          selectedOnly: true,
        });
      }
    }
    // One scale for the whole window (p99-capped), so a glow keeps its size
    // from hour to hour and an outlier cannot shrink everything else.
    const radius = scaleSqrt()
      .domain([
        0,
        Math.max(1, mode === 'window' ? metricModel.countryWindowP99 : metricModel.countryHourP99),
      ])
      .range([2.5, 14])
      .clamp(true);

    // Big marks underneath so every smaller one stays visible and clickable;
    // the selection is drawn last, on top of its neighbours.
    dots.sort(
      (a, b) =>
        Number(a.country === selectedCountry) - Number(b.country === selectedCountry) ||
        b.value - a.value,
    );
    const isSelected = (dot: CountryDot) => dot.country === selectedCountry;
    const isDimmed = (dot: CountryDot) => selectedCountry !== null && !isSelected(dot);
    const heat = heatLayer
      .selectAll<SVGGElement, CountryDot>('g')
      .data(dots, (dot) => dot.country)
      .join((enter) => {
        // Chromium's UA sheet outlines any focused SVG element on plain
        // `:focus` (not `:focus-visible`), so a mouse click or tap drew a
        // thick rectangle round the mark. Drop that outline and draw a
        // keyboard-only ring instead.
        const group = enter
          .append('g')
          .attr('role', 'button')
          .attr('tabindex', 0)
          .style('cursor', 'pointer')
          .style('outline', 'none');
        group
          .append('circle')
          .attr('data-layer', 'glow')
          .attr('fill', `url(#${gradientId}-glow)`)
          .attr('pointer-events', 'none');
        group.append('circle').attr('data-layer', 'core').attr('pointer-events', 'none');
        // A small core is a small target: the hit area never drops below 11px.
        group.append('circle').attr('data-layer', 'hit').attr('fill', 'transparent');
        group
          .append('circle')
          .attr('data-layer', 'selection-ring')
          .attr('fill', 'none')
          .attr('pointer-events', 'none')
          .attr('stroke', ORIGIN_COLORS.selected)
          .attr('stroke-width', 1.5);
        // A panel-colored halo under the accent keeps the ring legible over the
        // land matrix and neighbouring glows alike. Direct circles, not a
        // nested <g>: the join above selects every <g> in the layer.
        for (const [stroke, width] of [
          [ORIGIN_COLORS.panel, 4],
          [ORIGIN_COLORS.mark, 2],
        ] as const) {
          group
            .append('circle')
            .attr('data-layer', 'focus-ring')
            .attr('class', 'hidden [g:focus-visible>&]:inline')
            .attr('fill', 'none')
            .attr('pointer-events', 'none')
            .attr('stroke', stroke)
            .attr('stroke-width', width);
        }
        group.append('title');
        return group;
      })
      .order()
      .attr('aria-label', (dot) => `${atlasCountryName(atlas, dot.country)} request origin`)
      .attr('aria-pressed', (dot) => (isSelected(dot) ? 'true' : 'false'))
      .on('click', (_, dot) => onSelect({ type: 'country', country: dot.country }))
      .on('keydown', (event, dot) =>
        keyboardSelect(event, () => onSelect({ type: 'country', country: dot.country })),
      );
    // Dim the marks, not the group, so a keyboard focus ring stays at full strength.
    const markOpacity = (dot: CountryDot) => (isDimmed(dot) ? 0.35 : 1);
    const selectionRadius = (dot: CountryDot) => (dot.selectedOnly ? 9 : radius(dot.value) + 5);
    heat
      .select('circle[data-layer="glow"]')
      .attr('opacity', markOpacity)
      .attr('display', (dot) => (dot.selectedOnly ? 'none' : null))
      .attr('r', (dot) => radius(dot.value) * 3.2);
    heat
      .select('circle[data-layer="core"]')
      .attr('fill', (dot) => (isSelected(dot) ? ORIGIN_COLORS.selected : ORIGIN_COLORS.core))
      .attr('opacity', (dot) => markOpacity(dot) * (isSelected(dot) ? 1 : 0.92))
      .attr('display', (dot) => (dot.selectedOnly ? 'none' : null))
      .attr('r', (dot) => radius(dot.value));
    heat
      .select('circle[data-layer="hit"]')
      .attr('r', (dot) => Math.max(11, (dot.selectedOnly ? 9 : radius(dot.value)) + 4));
    heat
      .select('circle[data-layer="selection-ring"]')
      .attr('display', (dot) => (isSelected(dot) ? null : 'none'))
      .attr('r', selectionRadius);
    heat.each(function (dot) {
      select(this)
        .selectAll('circle[data-layer="focus-ring"]')
        .attr('r', selectionRadius(dot) + 4);
    });
    heat
      .select('title')
      .text((dot) =>
        dot.selectedOnly
          ? `${atlasCountryName(atlas, dot.country)}: no positive ${metricLabel(metricModel.metric)} ${mode === 'window' ? 'in selected window' : 'this hour'} · ${dot.requests.toLocaleString()} requests`
          : `${atlasCountryName(atlas, dot.country)}: ${Math.round(dot.value).toLocaleString()} ${metricLabel(metricModel.metric)} ${mode === 'window' ? 'in selected window' : 'this hour'} · ${dot.requests.toLocaleString()} requests`,
      );

    // A window total spans every hour of the day, so a single sun position
    // would mislabel it; only an hour drill-down has a day and night side.
    const selectedDate = new Date(snapshotTimestamp ?? '');
    const sun =
      mode === 'hour'
        ? sunPosition(Number.isNaN(selectedDate.getTime()) ? new Date(0) : selectedDate)
        : null;
    const daylight = sun ? dayNightGeometry(sun) : null;
    sunRef.current = sun;
    staticRedrawRef.current();
    for (const layer of [daySide, nightSide, terminator]) {
      layer.attr('display', daylight ? null : 'none');
      if (!daylight) layer.attr('d', null);
    }

    const isFront = (coordinate: [number, number]) =>
      geoDistance(centerOf(projection.rotate()), coordinate) * DEGREES < VISIBLE_LIMIT_DEG;
    // Direct labels: the selection first, then the biggest origins in view.
    const labelOrder = [...dots].sort(
      (a, b) => Number(isSelected(b)) - Number(isSelected(a)) || b.value - a.value,
    );
    const redrawDynamic = () => {
      if (daylight) {
        daySide.attr('d', path(daylight.day));
        nightSide.attr('d', path(daylight.night));
        terminator.attr('d', path(daylight.day));
      }
      heat
        .attr('transform', (dot) => {
          const point = projection(dot.coordinate);
          return point ? `translate(${point[0]},${point[1]})` : 'translate(-999,-999)';
        })
        .attr('display', (dot) => (isFront(dot.coordinate) ? null : 'none'));

      const marks = dots
        .filter((dot) => isFront(dot.coordinate))
        .map((dot) => {
          const [x, y] = projection(dot.coordinate) ?? [-999, -999];
          return { dot, x, y, r: dot.selectedOnly ? 9 : radius(dot.value) };
        });
      const byCountry = new Map(marks.map((mark) => [mark.dot.country, mark]));
      const valueText = (dot: CountryDot) => (dot.selectedOnly ? '—' : formatCompact(dot.value));
      const requests = labelOrder
        .filter((dot) => byCountry.has(dot.country))
        .slice(0, MAX_LABELS)
        .map((dot) => {
          const mark = byCountry.get(dot.country)!;
          return {
            key: dot.country,
            x: mark.x,
            y: mark.y,
            // Clear of the selection and keyboard focus rings.
            r: isSelected(dot) ? selectionRadius(dot) + 4 : mark.r + 3,
            width: estimateLabelWidth(atlasCountryName(atlas, dot.country), valueText(dot)),
          };
        });
      const placed = placeLabels(
        requests,
        sizeRef.current,
        marks.map(({ x, y, r }) => ({ x, y, r: r + 2 })),
      );
      const dotByCountry = new Map(dots.map((dot) => [dot.country, dot]));
      const labels = labelLayer
        .selectAll<SVGTextElement, (typeof placed)[number]>('text')
        .data(placed, (label) => label.key)
        .join((enter) => {
          const text = enter
            .append('text')
            .attr('paint-order', 'stroke')
            .attr('stroke', 'rgba(7,11,20,0.8)')
            .attr('stroke-width', 3)
            .attr('stroke-linejoin', 'round');
          text.append('tspan').attr('data-part', 'name');
          text.append('tspan').attr('data-part', 'value').attr('dx', 5).attr('fill', '#94a3b8');
          return text;
        })
        .attr('data-country', (label) => label.key)
        .attr('x', (label) => label.x)
        .attr('y', (label) => label.y)
        .attr('text-anchor', (label) => label.anchor);
      labels
        .select('tspan[data-part="name"]')
        .attr('fill', (label) =>
          selectedCountry !== null && label.key !== selectedCountry
            ? 'rgba(241,245,249,0.5)'
            : '#f1f5f9',
        )
        .text((label) => atlasCountryName(atlas, label.key));
      labels
        .select('tspan[data-part="value"]')
        .text((label) => valueText(dotByCountry.get(label.key)!));
    };
    dynamicRedrawRef.current = redrawDynamic;
    redrawDynamic();

    return () => {
      if (dynamicRedrawRef.current === redrawDynamic) {
        dynamicRedrawRef.current = () => undefined;
      }
    };
    // `land` too: a new land layer rebuilds the SVG, which must then be refilled.
  }, [
    atlas,
    gradientId,
    land,
    metricModel,
    mode,
    onSelect,
    selectedCountry,
    snapshotTimestamp,
    summary,
  ]);

  useEffect(() => {
    const projection = projectionRef.current;
    if (
      viewRequest.unlessVisible &&
      geoDistance(centerOf(projection.rotate()), viewRequest.unlessVisible) * DEGREES <
        COMFORTABLE_VIEW_DEG
    ) {
      return;
    }
    const target: [number, number, number] = [viewRequest.rotation[0], viewRequest.rotation[1], 0];
    const redraw = () => {
      staticRedrawRef.current();
      dynamicRedrawRef.current();
    };
    const reduceMotion =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!viewRequest.animate || reduceMotion) {
      projection.rotate(target);
      redraw();
      return;
    }
    const from = projection.rotate();
    // Take the short way around the antimeridian.
    const deltaLambda = ((target[0] - from[0] + 540) % 360) - 180;
    const deltaPhi = target[1] - from[1];
    const durationMs = 650;
    const startedAt = performance.now();
    let frame = 0;
    const step = (now: number) => {
      const progress = Math.min(1, (now - startedAt) / durationMs);
      const eased = progress < 0.5 ? 2 * progress * progress : 1 - (-2 * progress + 2) ** 2 / 2;
      projection.rotate([from[0] + deltaLambda * eased, from[1] + deltaPhi * eased, 0]);
      redraw();
      if (progress < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [viewRequest]);

  return (
    <div ref={stageRef} className="relative h-full min-w-0 overflow-hidden">
      <canvas
        ref={canvasRef}
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 h-full w-full"
      />
      <svg
        ref={svgRef}
        aria-label={
          mode === 'hour'
            ? 'Globe of IP-based request origins and day and night'
            : 'Globe of IP-based request origins'
        }
        className="relative block h-full w-full cursor-grab touch-none active:cursor-grabbing"
        role="group"
      />
      <p className="pointer-events-none absolute bottom-3 right-4 text-right text-[11px] text-slate-500">
        Glow size = {metricLabel(metricModel.metric)} · drag to turn
        {mode === 'hour' ? ' · lit land is in daylight' : ''}
      </p>
    </div>
  );
}
