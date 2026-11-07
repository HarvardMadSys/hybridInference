import type { Change } from '@/components/features/admin/confirm/ChangeList';
import { Select } from '@/components/ui/Select';
import type { ProviderRoute, RouteCapabilities, RuntimeModelCapabilities } from '@/lib/api/admin';

export type DeclaredChoice = '' | 'yes' | 'no';

/** The four capability fields as typed, before they are a valid declaration. */
export type CapabilityDraft = {
  contextLength: string;
  maxOutputLength: string;
  imageInput: DeclaredChoice;
  toolCalling: DeclaredChoice;
  // Input modalities the form has no field for (audio, video): sent back as
  // they came, so saving the other fields never drops one.
  otherInputModalities: string[];
};

export const emptyCapabilityDraft: CapabilityDraft = {
  contextLength: '',
  maxOutputLength: '',
  imageInput: '',
  toolCalling: '',
  otherInputModalities: [],
};

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

export type CapabilityDraftReading = {
  payload: RuntimeModelCapabilities | null;
  contextLengthError: string | null;
  maxOutputLengthError: string | null;
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
  const payload: RuntimeModelCapabilities | null =
    contextLength !== null &&
    maxOutputLength !== null &&
    maxOutputLengthError === null &&
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
        }
      : null;
  return { payload, contextLengthError, maxOutputLengthError };
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
    modalities(a) === modalities(b)
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
    [...a.otherInputModalities].sort().join(',') === [...b.otherInputModalities].sort().join(',')
  );
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
  ].join(' · ');
}

/** One row per capability, as the form names them, for listing changes. */
export function capabilityRows(capabilities: RouteCapabilities): Change[] {
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

export function RouteCapabilityFields({
  idPrefix,
  draft,
  reading,
  onChange,
  help,
  disabled = false,
}: {
  idPrefix: string;
  draft: CapabilityDraft;
  reading: CapabilityDraftReading;
  onChange: (change: Partial<CapabilityDraft>) => void;
  help: string;
  /** While the values are being written: they stay what was saved. */
  disabled?: boolean;
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
    </div>
  );
}
