import type { RouteCapabilities, RuntimeModelCapabilities } from '@/lib/api/admin';

export type DeclaredChoice = '' | 'yes' | 'no';

/** The four capability fields as typed, before they are a valid declaration. */
export type CapabilityDraft = {
  contextLength: string;
  maxOutputLength: string;
  imageInput: DeclaredChoice;
  toolCalling: DeclaredChoice;
};

export const emptyCapabilityDraft: CapabilityDraft = {
  contextLength: '',
  maxOutputLength: '',
  imageInput: '',
  toolCalling: '',
};

/** A draft holding what a route serves with now, for editing or as a new route's start. */
export function capabilityDraftFrom(capabilities: RouteCapabilities): CapabilityDraft {
  return {
    contextLength: String(capabilities.context_length),
    maxOutputLength: String(capabilities.max_output_length),
    imageInput: capabilities.input_modalities.includes('image') ? 'yes' : 'no',
    toolCalling: capabilities.supports_tools ? 'yes' : 'no',
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
          input_modalities: draft.imageInput === 'yes' ? ['text', 'image'] : ['text'],
          supports_tools: draft.toolCalling === 'yes',
        }
      : null;
  return { payload, contextLengthError, maxOutputLengthError };
}

/** "131,072 context · 65,536 output · text and images · tools" */
export function describeCapabilities(capabilities: RouteCapabilities) {
  return [
    `${capabilities.context_length.toLocaleString('en-US')} context`,
    `${capabilities.max_output_length.toLocaleString('en-US')} output`,
    capabilities.input_modalities.includes('image') ? 'text and images' : 'text only',
    capabilities.supports_tools ? 'tools' : 'no tools',
  ].join(' · ');
}

const FIELD_CLASS =
  'mt-1 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] focus:border-gray-400 focus:outline-none';

export function RouteCapabilityFields({
  idPrefix,
  draft,
  reading,
  onChange,
  help,
}: {
  idPrefix: string;
  draft: CapabilityDraft;
  reading: CapabilityDraftReading;
  onChange: (change: Partial<CapabilityDraft>) => void;
  help: string;
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
          <select
            id={`${idPrefix}-image-input`}
            value={draft.imageInput}
            onChange={(event) => onChange({ imageInput: event.target.value as DeclaredChoice })}
            className={FIELD_CLASS}
            required
          >
            <option value="">Choose…</option>
            <option value="no">Text only</option>
            <option value="yes">Accepts images</option>
          </select>
        </div>
        <div>
          <label
            className="text-[12px] font-medium text-gray-500"
            htmlFor={`${idPrefix}-tool-calling`}
          >
            Tool calling
          </label>
          <select
            id={`${idPrefix}-tool-calling`}
            value={draft.toolCalling}
            onChange={(event) => onChange({ toolCalling: event.target.value as DeclaredChoice })}
            className={FIELD_CLASS}
            required
          >
            <option value="">Choose…</option>
            <option value="no">Not supported</option>
            <option value="yes">Supported</option>
          </select>
        </div>
      </div>
      <p className="mt-1 text-[11px] leading-5 text-gray-400">{help}</p>
    </div>
  );
}
