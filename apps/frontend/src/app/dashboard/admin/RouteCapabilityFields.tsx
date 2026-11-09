import type { Change } from '@/components/features/admin/confirm/ChangeList';
import { Select } from '@/components/ui/Select';
import type { ProviderRoute, RouteCapabilities, RuntimeModelCapabilities } from '@/lib/api/admin';

export type DeclaredChoice = '' | 'yes' | 'no';

/** One row of a reasoning-effort map: the level clients send, and what the upstream gets. */
export type EffortRow = { publicLevel: string; upstreamLevel: string };

/** The capability fields as typed, before they are a valid declaration. */
export type CapabilityDraft = {
  contextLength: string;
  maxOutputLength: string;
  imageInput: DeclaredChoice;
  toolCalling: DeclaredChoice;
  // Input modalities the form has no field for (audio, video): sent back as
  // they came, so saving the other fields never drops one.
  otherInputModalities: string[];
  // The route's reasoning-effort map, one row per public level, in offer order.
  // No rows: the route keeps the map it has by default (or none).
  effortRows: EffortRow[];
};

export const emptyCapabilityDraft: CapabilityDraft = {
  contextLength: '',
  maxOutputLength: '',
  imageInput: '',
  toolCalling: '',
  otherInputModalities: [],
  effortRows: [],
};

type EffortMap = Record<string, string>;

function effortRowsFrom(map: EffortMap | null | undefined): EffortRow[] {
  return Object.entries(map ?? {}).map(([publicLevel, upstreamLevel]) => ({
    publicLevel,
    upstreamLevel,
  }));
}

/** Whether two maps send the same levels the same way, in the same order (none = empty). */
export function sameEffortMap(a: EffortMap | null | undefined, b: EffortMap | null | undefined) {
  const left = Object.entries(a ?? {});
  const right = Object.entries(b ?? {});
  return (
    left.length === right.length &&
    left.every(([level, value], index) => right[index][0] === level && right[index][1] === value)
  );
}

/** "low → high, high → xhigh, max → max" */
export function describeEffortMap(map: EffortMap | null | undefined) {
  const entries = Object.entries(map ?? {});
  return entries.length
    ? entries.map(([level, value]) => `${level} → ${value}`).join(', ')
    : 'No mapping';
}

// The backend's rule (serving.adapters.reasoning_effort): a short token.
const EFFORT_LEVEL_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$/;
const MAX_EFFORT_ROWS = 16;

/** The rows as a map, or why they are not one. Blank rows are ignored. */
export function readEffortRows(rows: EffortRow[]): {
  map: EffortMap | null;
  error: string | null;
} {
  const filled = rows
    .map((row) => ({
      publicLevel: row.publicLevel.trim(),
      upstreamLevel: row.upstreamLevel.trim(),
    }))
    .filter((row) => row.publicLevel || row.upstreamLevel);
  if (filled.length === 0) return { map: null, error: null };
  if (filled.length > MAX_EFFORT_ROWS) {
    return { map: null, error: `Map at most ${MAX_EFFORT_ROWS} levels.` };
  }
  const map: EffortMap = {};
  for (const row of filled) {
    if (!row.publicLevel || !row.upstreamLevel) {
      return { map: null, error: 'Fill in both levels of each row, or remove the row.' };
    }
    if (!EFFORT_LEVEL_RE.test(row.publicLevel) || !EFFORT_LEVEL_RE.test(row.upstreamLevel)) {
      return {
        map: null,
        error: 'A level is one short word: letters, digits, "_", "." or "-" (up to 32).',
      };
    }
    if (row.publicLevel in map) {
      return { map: null, error: `Public level "${row.publicLevel}" is listed twice.` };
    }
    map[row.publicLevel] = row.upstreamLevel;
  }
  return { map, error: null };
}

/** A draft holding what a route serves with now, for editing or as a new route's start. */
export function capabilityDraftFrom(capabilities: RouteCapabilities): CapabilityDraft {
  return {
    contextLength: String(capabilities.context_length),
    maxOutputLength: String(capabilities.max_output_length),
    imageInput: capabilities.input_modalities.includes('image') ? 'yes' : 'no',
    toolCalling: capabilities.supports_tools ? 'yes' : 'no',
    otherInputModalities: capabilities.input_modalities.filter(
      (modality) => modality !== 'text' && modality !== 'image',
    ),
    effortRows: effortRowsFrom(capabilities.reasoning_effort_map),
  };
}

// The backend's bound (RUNTIME_MODEL_MAX_TOKENS): the largest window a model ships with.
const MAX_DECLARED_TOKENS = 10_000_000;
const TOKEN_COUNT_ERROR = `Enter a whole number of tokens from 1 to ${MAX_DECLARED_TOKENS.toLocaleString('en-US')}.`;

function tokenCountOrNull(value: string) {
  const trimmed = value.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const parsed = Number.parseInt(trimmed, 10);
  return parsed > 0 && parsed <= MAX_DECLARED_TOKENS ? parsed : null;
}

type CapabilityDraftReading = {
  payload: RuntimeModelCapabilities | null;
  contextLengthError: string | null;
  maxOutputLengthError: string | null;
  effortMapError: string | null;
};

// One reading of the four fields, used both for the inline messages and to
// gate the form's buttons, so a field can never block a form without saying why.
// An empty field has no message: it is visibly unanswered.
export function readCapabilityDraft(draft: CapabilityDraft): CapabilityDraftReading {
  const contextLength = tokenCountOrNull(draft.contextLength);
  const maxOutputLength = tokenCountOrNull(draft.maxOutputLength);
  const contextLengthError =
    draft.contextLength.trim() && contextLength === null ? TOKEN_COUNT_ERROR : null;
  let maxOutputLengthError =
    draft.maxOutputLength.trim() && maxOutputLength === null ? TOKEN_COUNT_ERROR : null;
  if (contextLength !== null && maxOutputLength !== null && maxOutputLength > contextLength) {
    maxOutputLengthError = 'Max output cannot exceed the context length.';
  }
  const effort = readEffortRows(draft.effortRows);
  const payload: RuntimeModelCapabilities | null =
    contextLength !== null &&
    maxOutputLength !== null &&
    maxOutputLengthError === null &&
    effort.error === null &&
    draft.imageInput &&
    draft.toolCalling
      ? {
          context_length: contextLength,
          max_output_length: maxOutputLength,
          input_modalities: [
            'text',
            ...(draft.imageInput === 'yes' ? ['image'] : []),
            ...draft.otherInputModalities,
          ],
          supports_tools: draft.toolCalling === 'yes',
          // No rows: no map is sent, which keeps the route's default one.
          ...(effort.map ? { reasoning_effort_map: effort.map } : {}),
        }
      : null;
  return { payload, contextLengthError, maxOutputLengthError, effortMapError: effort.error };
}

/**
 * Whether two declarations are the same as this form shows them. Text input is
 * implied: the form always sends it, so a declaration without it (which
 * models.yaml does not refuse) is not a different one.
 */
export function sameCapabilities(a: RouteCapabilities, b: RouteCapabilities) {
  const modalities = (capabilities: RouteCapabilities) =>
    [...new Set(capabilities.input_modalities.filter((modality) => modality !== 'text'))]
      .sort()
      .join(',');
  return (
    a.context_length === b.context_length &&
    a.max_output_length === b.max_output_length &&
    a.supports_tools === b.supports_tools &&
    modalities(a) === modalities(b) &&
    sameEffortMap(a.reasoning_effort_map, b.reasoning_effort_map)
  );
}

/**
 * Whether two drafts hold the same answers: "8192" and "08192" are one
 * count, and a value the form would refuse is compared as typed.
 */
export function sameCapabilityDraft(a: CapabilityDraft, b: CapabilityDraft) {
  const sameCount = (x: string, y: string) =>
    x.trim() === y.trim() ||
    (/^\d+$/.test(x.trim()) && /^\d+$/.test(y.trim()) && Number(x) === Number(y));
  return (
    sameCount(a.contextLength, b.contextLength) &&
    sameCount(a.maxOutputLength, b.maxOutputLength) &&
    a.imageInput === b.imageInput &&
    a.toolCalling === b.toolCalling &&
    [...a.otherInputModalities].sort().join(',') === [...b.otherInputModalities].sort().join(',') &&
    sameEffortRows(a.effortRows, b.effortRows)
  );
}

/** Whether two sets of rows hold the same answers, blank rows aside. */
function sameEffortRows(a: EffortRow[], b: EffortRow[]) {
  const answers = (rows: EffortRow[]) =>
    rows
      .map((row) => [row.publicLevel.trim(), row.upstreamLevel.trim()])
      .filter(([level, value]) => level || value)
      .map(([level, value]) => `${level}=${value}`)
      .join(',');
  return answers(a) === answers(b);
}

/** Where a route's default capabilities come from, as a label. */
export function capabilityDefaultsLabel(route: Pick<ProviderRoute, 'source'>) {
  return route.source === 'runtime'
    ? 'Default from when this route was added'
    : 'Default from models.yaml';
}

/** "text, images and audio" */
function listWords(words: string[]) {
  return words.length > 1
    ? `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`
    : words.join('');
}

/** "131,072 context · 65,536 output · text and images · tools" */
export function describeCapabilities(capabilities: RouteCapabilities) {
  const media = capabilities.input_modalities
    .filter((modality) => modality !== 'text')
    .map((modality) => (modality === 'image' ? 'images' : modality));
  return [
    `${capabilities.context_length.toLocaleString('en-US')} context`,
    `${capabilities.max_output_length.toLocaleString('en-US')} output`,
    media.length ? listWords(['text', ...media]) : 'text only',
    capabilities.supports_tools ? 'tools' : 'no tools',
    ...(capabilities.reasoning_effort_map && Object.keys(capabilities.reasoning_effort_map).length
      ? [`effort ${Object.keys(capabilities.reasoning_effort_map).join(', ')}`]
      : []),
  ].join(' · ');
}

/** One row per capability, as the form names them, for listing changes. */
function capabilityRows(capabilities: RouteCapabilities): Change[] {
  const others = capabilities.input_modalities.filter(
    (modality) => modality !== 'text' && modality !== 'image',
  );
  return [
    {
      label: 'Context length',
      after: `${capabilities.context_length.toLocaleString('en-US')} tokens`,
    },
    {
      label: 'Max output',
      after: `${capabilities.max_output_length.toLocaleString('en-US')} tokens`,
    },
    {
      label: 'Image input',
      after: capabilities.input_modalities.includes('image') ? 'Accepts images' : 'Text only',
    },
    ...(others.length ? [{ label: 'Other input', after: listWords(others) }] : []),
    { label: 'Tool calling', after: capabilities.supports_tools ? 'Supported' : 'Not supported' },
    { label: 'Reasoning effort', after: describeEffortMap(capabilities.reasoning_effort_map) },
  ];
}

/** The capabilities that differ, `before → after`, in the form's order. */
export function capabilityChanges(before: RouteCapabilities, after: RouteCapabilities): Change[] {
  const old = new Map(capabilityRows(before).map((row) => [row.label, row.after]));
  const next = capabilityRows(after);
  const labels = new Set(next.map((row) => row.label));
  return [
    ...next
      .filter((row) => old.get(row.label) !== row.after)
      .map((row) => ({
        label: row.label,
        before: old.get(row.label) ?? '(none)',
        after: row.after,
      })),
    ...[...old]
      .filter(([label]) => !labels.has(label))
      .map(([label, value]) => ({ label, before: value, after: '(none)' })),
  ];
}

const FIELD_CLASS =
  'mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none';

/** What the form knows about an existing route's reasoning effort. */
export type RouteEffortContext = {
  /** The route's adapter sends `reasoning_effort`; otherwise no map can apply. */
  mappable: boolean;
  /** The public levels the live route offers clients now (`null`: unknown). */
  published?: string[] | null;
  /** The map the route has without a console value, kept when no rows are set. */
  defaultMap?: EffortMap | null;
  presetLabel?: string | null;
  source?: ProviderRoute['reasoning_effort_source'];
  onRestore?: () => void;
};

function ReasoningEffortFields({
  idPrefix,
  rows,
  error,
  onChange,
  context,
  disabled,
}: {
  idPrefix: string;
  rows: EffortRow[];
  error: string | null;
  onChange: (rows: EffortRow[]) => void;
  context?: RouteEffortContext;
  disabled: boolean;
}) {
  const labelId = `${idPrefix}-effort-label`;
  if (context && !context.mappable) {
    return (
      <div className="mt-4">
        <p id={labelId} className="text-[12px] font-medium text-gray-500">
          Reasoning effort levels
        </p>
        <p className="mt-1 text-[11px] leading-5 text-gray-500">
          Effort capability has not been declared for this route. Its adapter has no
          reasoning_effort mapping.
        </p>
      </div>
    );
  }
  const update = (index: number, change: Partial<EffortRow>) =>
    onChange(rows.map((row, at) => (at === index ? { ...row, ...change } : row)));
  const customMap = readEffortRows(rows).map;
  const defaultMap = context?.defaultMap;
  const levels = customMap
    ? Object.keys(customMap)
    : defaultMap
      ? Object.keys(defaultMap)
      : context?.published;
  const source = customMap
    ? context?.source === 'configured'
      ? 'Route configuration'
      : 'Custom mapping'
    : context?.presetLabel
      ? `Preset · ${context.presetLabel}`
      : context?.source === 'configured'
        ? 'Route configuration'
        : null;
  return (
    <div className="mt-4" role="group" aria-labelledby={labelId}>
      <p id={labelId} className="text-[12px] font-medium text-gray-500">
        Reasoning effort levels
      </p>
      <p className="mt-1 text-[12px] leading-5 text-gray-700">
        {levels == null
          ? 'Effort capability has not been declared for this route.'
          : levels.length
            ? `Client choices on this route: ${levels.join(', ')}`
            : 'This route offers no effort choices. The model keeps its usual thinking behavior.'}
      </p>
      {source && <p className="mt-1 text-[11px] text-gray-500">{source}</p>}
      <details className="mt-2 rounded-lg border border-gray-200 px-3 py-2">
        <summary className="cursor-pointer text-[12px] font-medium text-gray-700">
          Advanced mapping
        </summary>
        <p className="mt-2 text-[11px] leading-5 text-gray-500">
          Map each public choice to the value sent upstream as reasoning_effort. A custom mapping
          replaces the default and limits the route to the public levels listed here.
        </p>
        {rows.length === 0 && defaultMap && (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className="text-[12px] text-gray-700">{describeEffortMap(defaultMap)}</span>
            <button
              type="button"
              onClick={() => onChange(effortRowsFrom(defaultMap))}
              disabled={disabled}
              className="rounded-lg border border-gray-200 px-3 py-1.5 text-[12px] text-gray-700 hover:bg-gray-50"
            >
              Customize mapping
            </button>
          </div>
        )}
        {rows.map((row, index) => (
          <div key={index} className="mt-2 flex items-center gap-2">
            <input
              aria-label={`Public level ${index + 1}`}
              placeholder="Public level, e.g. high"
              value={row.publicLevel}
              onChange={(event) => update(index, { publicLevel: event.target.value })}
              disabled={disabled}
              className={`${FIELD_CLASS} mt-0`}
            />
            <span aria-hidden className="text-gray-400">
              →
            </span>
            <input
              aria-label={`Upstream value ${index + 1}`}
              placeholder="Sent upstream as"
              value={row.upstreamLevel}
              onChange={(event) => update(index, { upstreamLevel: event.target.value })}
              disabled={disabled}
              className={`${FIELD_CLASS} mt-0`}
            />
            <button
              type="button"
              onClick={() => onChange(rows.filter((_row, at) => at !== index))}
              disabled={disabled}
              className="shrink-0 rounded-lg border border-gray-200 px-2 py-2 text-[12px] text-gray-600 hover:bg-gray-50"
            >
              Remove
            </button>
          </div>
        ))}
        {(rows.length > 0 || !defaultMap) && (
          <button
            type="button"
            onClick={() => onChange([...rows, { publicLevel: '', upstreamLevel: '' }])}
            disabled={disabled}
            className="mt-2 rounded-lg border border-gray-200 px-3 py-1.5 text-[12px] text-gray-700 hover:bg-gray-50"
          >
            Add level
          </button>
        )}
        {error && <p className="mt-1 text-[11px] leading-5 text-red-600">{error}</p>}
        {context?.onRestore && (
          <button
            type="button"
            onClick={context.onRestore}
            disabled={disabled}
            className="ml-2 mt-2 rounded px-1 text-[12px] font-medium text-gray-900 hover:bg-gray-100"
          >
            Use default effort settings
          </button>
        )}
      </details>
    </div>
  );
}

export function RouteCapabilityFields({
  idPrefix,
  draft,
  reading,
  onChange,
  help,
  disabled = false,
  effort,
}: {
  idPrefix: string;
  draft: CapabilityDraft;
  reading: CapabilityDraftReading;
  onChange: (change: Partial<CapabilityDraft>) => void;
  help: string;
  /** While the values are being written: they stay what was saved. */
  disabled?: boolean;
  /** An existing route's effort facts; omitted for a route being added. */
  effort?: RouteEffortContext;
}) {
  return (
    <div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <label
            className="text-[12px] font-medium text-gray-500"
            htmlFor={`${idPrefix}-context-length`}
          >
            Context length (tokens)
          </label>
          <input
            id={`${idPrefix}-context-length`}
            type="number"
            min="1"
            step="1"
            value={draft.contextLength}
            onChange={(event) => onChange({ contextLength: event.target.value })}
            disabled={disabled}
            className={FIELD_CLASS}
            required
          />
          {reading.contextLengthError && (
            <p className="mt-1 text-[11px] leading-5 text-red-600">{reading.contextLengthError}</p>
          )}
        </div>
        <div>
          <label
            className="text-[12px] font-medium text-gray-500"
            htmlFor={`${idPrefix}-max-output-length`}
          >
            Max output (tokens)
          </label>
          <input
            id={`${idPrefix}-max-output-length`}
            type="number"
            min="1"
            step="1"
            value={draft.maxOutputLength}
            onChange={(event) => onChange({ maxOutputLength: event.target.value })}
            disabled={disabled}
            className={FIELD_CLASS}
            required
          />
          {reading.maxOutputLengthError && (
            <p className="mt-1 text-[11px] leading-5 text-red-600">
              {reading.maxOutputLengthError}
            </p>
          )}
        </div>
        <div>
          <label
            className="text-[12px] font-medium text-gray-500"
            htmlFor={`${idPrefix}-image-input`}
          >
            Image input
          </label>
          <Select<DeclaredChoice>
            id={`${idPrefix}-image-input`}
            value={draft.imageInput}
            onValueChange={(imageInput) => onChange({ imageInput })}
            disabled={disabled}
            // "" is unanswered, not "no": the form stays invalid until one is chosen.
            options={[
              { value: '', label: 'Choose…' },
              { value: 'no', label: 'Text only' },
              { value: 'yes', label: 'Accepts images' },
            ]}
            aria-required
            size="lg"
            className="mt-1 w-full"
          />
        </div>
        <div>
          <label
            className="text-[12px] font-medium text-gray-500"
            htmlFor={`${idPrefix}-tool-calling`}
          >
            Tool calling
          </label>
          <Select<DeclaredChoice>
            id={`${idPrefix}-tool-calling`}
            value={draft.toolCalling}
            onValueChange={(toolCalling) => onChange({ toolCalling })}
            disabled={disabled}
            options={[
              { value: '', label: 'Choose…' },
              { value: 'no', label: 'Not supported' },
              { value: 'yes', label: 'Supported' },
            ]}
            aria-required
            size="lg"
            className="mt-1 w-full"
          />
        </div>
      </div>
      {draft.otherInputModalities.length > 0 && (
        <p className="mt-1 text-[11px] leading-5 text-gray-500">
          Also accepts {listWords(draft.otherInputModalities)} input, which this form leaves as
          declared.
        </p>
      )}
      <p className="mt-1 text-[11px] leading-5 text-gray-400">{help}</p>
      <ReasoningEffortFields
        idPrefix={idPrefix}
        rows={draft.effortRows}
        error={reading.effortMapError}
        onChange={(effortRows) => onChange({ effortRows })}
        context={effort}
        disabled={disabled}
      />
    </div>
  );
}
