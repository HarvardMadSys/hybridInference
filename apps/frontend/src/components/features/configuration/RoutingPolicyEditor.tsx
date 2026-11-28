'use client';

import { useId, useMemo, useState, type ReactNode } from 'react';

import {
  BUTTON_GHOST,
  BUTTON_SECONDARY,
  FIELD_INVALID,
  TEXT_FIELD,
} from '@/components/features/admin/adminStyles';
import type { ConfigEntry } from '@/lib/api/config';

import { currentInput, settleDraft, type ConfigDraft } from './configDrafts';
import { documentText, parseDocument, sameJson } from './documentFields';
import { NOT_A_LOGIN_FIELD } from './notALoginField';
import {
  DEPLOYMENT_SECTIONS,
  EMPTY_FORM,
  FIELD_IDS,
  POLICY_DEFAULTS,
  emptyRow,
  flaggedBy,
  policyForm,
  policyFromForm,
  policySummary,
  routerLabel,
  routerOptions,
  rowFieldId,
  type DeploymentRow,
  type DeploymentSection,
  type PolicyForm,
} from './routingPolicy';

type View = 'policy' | 'json';

const LABEL = 'mb-1 block text-[12px] font-medium text-gray-700';
const HINT = 'mt-1 text-[11px] text-gray-500';
const ERROR = 'mt-0.5 text-[11px] text-red-600';
const SECTION_TITLE = 'mb-2 text-[11px] font-semibold uppercase tracking-wide text-gray-500';
const VARIABLE = 'ml-1.5 font-mono text-[11px] font-normal text-gray-400';

const SECTION_NAMES: Record<DeploymentSection, { title: string; one: string }> = {
  local_deployment: { title: 'Local deployments', one: 'local deployment' },
  remote_deployment: { title: 'Remote deployments', one: 'remote deployment' },
};

/**
 * The routing policy's editor: one card with the default router, the Fixed
 * router's local/remote split and the endpoints it counts on each side, and
 * the local deployments' health checks. Each value has a readable label with
 * its key beside it, and a field left empty uses the backend's default. Keys
 * the card does not edit are kept, and the whole document is a click away as
 * JSON. The draft is the policy's JSON text, so saving, discarding and
 * conflicts work as they do for any setting.
 */
export function RoutingPolicyEditor({
  entry,
  draft,
  onChange,
  json,
  labelId,
  describedBy,
  serverError,
  disabled = false,
}: {
  entry: ConfigEntry;
  draft: ConfigDraft | undefined;
  onChange: (key: string, draft: ConfigDraft | undefined) => void;
  /** The policy's JSON view: the setting's own text editor. */
  json: ReactNode;
  labelId: string;
  describedBy?: string;
  serverError?: string;
  disabled?: boolean;
}) {
  const text = String(currentInput(entry, draft));
  const form = useMemo(() => policyForm(text), [text]);
  const [view, setView] = useState<View>(() => (form !== null ? 'policy' : 'json'));
  const shown: View = view === 'policy' && form !== null ? 'policy' : 'json';

  const choose = (next: View) => {
    // A field that is not a value of its type stays behind in the card: the
    // JSON shows, and saves, the policy the other fields made.
    if (next === 'json' && draft?.kind === 'value' && draft.error) {
      onChange(entry.key, settleDraft(entry, { kind: 'value', value: draft.value }));
    }
    setView(next);
  };

  let note = 'The whole policy as JSON.';
  if (shown === 'policy')
    note = 'Empty fields use the default. Changes apply after Save and a restart.';
  else if (parseDocument(text) === null) {
    note = 'This text is not a JSON object; correct it to edit the policy as a card.';
  } else if (form === null) {
    note = 'This policy holds a value the card cannot show exactly; edit it as JSON.';
  }

  return (
    <div className="max-w-4xl">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <p className="text-[11px] text-gray-500">{note}</p>
        <div
          role="group"
          aria-label={`Edit ${entry.key} as`}
          className="inline-flex shrink-0 rounded-md border border-gray-200 bg-gray-50 p-0.5 text-[12px]"
        >
          {(['policy', 'json'] as const).map((option) => (
            <button
              key={option}
              type="button"
              aria-pressed={shown === option}
              disabled={option === 'policy' && form === null}
              onClick={() => choose(option)}
              className={`rounded px-2.5 py-0.5 font-medium transition disabled:cursor-not-allowed disabled:opacity-40 ${
                shown === option
                  ? 'bg-white text-gray-900 shadow-sm'
                  : 'text-gray-500 hover:text-gray-900'
              }`}
            >
              {option === 'policy' ? 'Policy' : 'JSON'}
            </button>
          ))}
        </div>
      </div>
      {shown === 'policy' ? (
        <PolicyCard
          entry={entry}
          draft={draft}
          onChange={onChange}
          labelId={labelId}
          describedBy={describedBy}
          serverError={serverError}
          disabled={disabled}
        />
      ) : (
        json
      )}
    </div>
  );
}

/** What was typed into the card, and the policy it was typed onto. */
interface Session {
  /** The draft this was typed onto, or that the last edit here made. */
  draft: ConfigDraft | undefined;
  /** The saved policy's text then, which a reload or another save can replace. */
  saved: string;
  /** The policy's text when typing began. */
  base: string;
  form: PolicyForm;
}

function startSession(entry: ConfigEntry, draft: ConfigDraft | undefined): Session {
  const base = String(currentInput(entry, draft));
  return {
    draft,
    saved: String(currentInput(entry, undefined)),
    base,
    // The card is only shown for a policy it can show.
    form: policyForm(base) ?? EMPTY_FORM,
  };
}

function problemSummary(errors: Record<string, string>): string {
  const ids = Object.keys(errors);
  if (ids.length === 1) return `${ids[0]}: ${errors[ids[0]]}`;
  return `${ids.length} fields are not valid: ${ids.join(', ')}.`;
}

function PolicyCard({
  entry,
  draft,
  onChange,
  labelId,
  describedBy,
  serverError,
  disabled,
}: {
  entry: ConfigEntry;
  draft: ConfigDraft | undefined;
  onChange: (key: string, draft: ConfigDraft | undefined) => void;
  labelId: string;
  describedBy?: string;
  serverError?: string;
  disabled: boolean;
}) {
  const baseId = useId();
  const savedText = String(currentInput(entry, undefined));
  const [session, setSession] = useState<Session>(() => startSession(entry, draft));
  let current = session;
  // Changed somewhere else -- Discard, the JSON view, a save, a reload -- or,
  // with nothing unsaved here, a newer saved policy: the card starts again
  // from the policy as it is now.
  if (session.draft !== draft || (draft === undefined && session.saved !== savedText)) {
    current = startSession(entry, draft);
    setSession(current);
  }
  const { form } = current;
  const base = useMemo(() => parseDocument(current.base) ?? {}, [current.base]);
  const errors = useMemo(() => policyFromForm(base, form).errors, [base, form]);

  const update = (next: PolicyForm) => {
    const result = policyFromForm(base, next);
    const saved = parseDocument(savedText);
    const invalid = Object.keys(result.errors).length > 0;
    let nextDraft: ConfigDraft | undefined;
    if (!invalid && saved !== null && sameJson(result.document, saved)) {
      nextDraft = undefined;
    } else {
      nextDraft = {
        kind: 'value',
        // A policy whose values did not change keeps its own text.
        value: sameJson(result.document, base) ? current.base : documentText(result.document),
        ...(invalid ? { error: problemSummary(result.errors) } : {}),
      };
    }
    setSession({ draft: nextDraft, saved: savedText, base: current.base, form: next });
    onChange(entry.key, nextDraft);
  };
  const set = <K extends keyof PolicyForm>(key: K, value: PolicyForm[K]) =>
    update({ ...form, [key]: value });
  const setRows = (section: DeploymentSection, rows: DeploymentRow[]) => set(section, rows);

  const fid = (id: string) => `${baseId}-${id.replace(/\//g, '-')}`;
  const router = form.defaultRouter;
  const routerFlagged = flaggedBy(serverError, FIELD_IDS.defaultRouter);

  return (
    <div
      role="group"
      aria-labelledby={labelId}
      aria-describedby={describedBy}
      className="space-y-5 rounded-md border border-gray-200 p-4"
    >
      <ul
        aria-label="What this policy does"
        className="space-y-0.5 rounded-md bg-gray-50 px-3 py-2 text-[12px] text-gray-700"
      >
        {policySummary(form).map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>

      <section>
        <h4 className={SECTION_TITLE}>Default router</h4>
        <div className="max-w-sm">
          <label htmlFor={fid(FIELD_IDS.defaultRouter)} className={LABEL}>
            Router
            <span className={VARIABLE}>default_router</span>
          </label>
          <select
            id={fid(FIELD_IDS.defaultRouter)}
            value={router}
            disabled={disabled}
            onChange={(event) => set('defaultRouter', event.target.value)}
            aria-invalid={routerFlagged || undefined}
            className={`${TEXT_FIELD} ${routerFlagged ? FIELD_INVALID : ''}`}
          >
            <option value="">Default ({routerLabel(POLICY_DEFAULTS.default_router)})</option>
            {routerOptions(router).map((name) => (
              <option key={name} value={name}>
                {routerLabel(name)}
              </option>
            ))}
          </select>
          <p className={HINT}>
            Used by every model whose catalog entry names no router. ENABLE_ROUTEWISE turns a Fixed
            default into RouteWise.
          </p>
        </div>
      </section>

      <section>
        <h4 className={SECTION_TITLE}>Local and remote split</h4>
        <NumberField
          id={fid(FIELD_IDS.localFraction)}
          label="Local share"
          variable="routing_parameter.local_fraction"
          value={form.localFraction}
          placeholder={String(POLICY_DEFAULTS.local_fraction)}
          decimal
          error={errors[FIELD_IDS.localFraction]}
          flagged={flaggedBy(serverError, FIELD_IDS.localFraction)}
          disabled={disabled}
          hint="From 0 to 1. With the Fixed router, the share of a model's traffic its local deployments get when the model also has remote ones. Only the endpoints listed below count. Applied at startup, and only on a gateway without a database: with one, route weights come from Admin Routing and this split has no effect."
          onChange={(value) => set('localFraction', value)}
        />
        <div className="mt-4 space-y-4">
          {DEPLOYMENT_SECTIONS.map((section) => (
            <DeploymentList
              key={section}
              section={section}
              rows={form[section]}
              fid={fid}
              errors={errors}
              serverError={serverError}
              disabled={disabled}
              onChange={(rows) => setRows(section, rows)}
            />
          ))}
        </div>
      </section>

      <section>
        <h4 className={SECTION_TITLE}>Health checks</h4>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <NumberField
            id={fid(FIELD_IDS.healthCheck)}
            label="Check every (seconds)"
            variable="health_check"
            value={form.healthCheck}
            placeholder={String(POLICY_DEFAULTS.health_check)}
            error={errors[FIELD_IDS.healthCheck]}
            flagged={flaggedBy(serverError, FIELD_IDS.healthCheck)}
            disabled={disabled}
            hint="Asks each local deployment's /health and reports what it finds in the log and on GET /routing. It does not change where requests go. 0 turns checks off."
            onChange={(value) => set('healthCheck', value)}
          />
          <NumberField
            id={fid(FIELD_IDS.timeout)}
            label="Check timeout (seconds)"
            variable="timeout"
            value={form.timeout}
            placeholder={String(POLICY_DEFAULTS.timeout)}
            error={errors[FIELD_IDS.timeout]}
            flagged={flaggedBy(serverError, FIELD_IDS.timeout)}
            disabled={disabled}
            hint="How long one health check waits for an answer."
            onChange={(value) => set('timeout', value)}
          />
        </div>
      </section>
    </div>
  );
}

function NumberField({
  id,
  label,
  variable,
  value,
  placeholder,
  decimal = false,
  error,
  flagged,
  disabled,
  hint,
  onChange,
}: {
  id: string;
  label: string;
  variable: string;
  value: string;
  placeholder: string;
  decimal?: boolean;
  error?: string;
  flagged: boolean;
  disabled: boolean;
  hint: string;
  onChange: (value: string) => void;
}) {
  const invalid = Boolean(error) || flagged;
  return (
    <div className="max-w-sm">
      <label htmlFor={id} className={LABEL}>
        {label}
        <span className={VARIABLE}>{variable}</span>
      </label>
      <input
        id={id}
        type="text"
        inputMode={decimal ? 'decimal' : 'numeric'}
        value={value}
        placeholder={placeholder}
        disabled={disabled}
        spellCheck={false}
        autoComplete="off"
        {...NOT_A_LOGIN_FIELD}
        aria-invalid={invalid || undefined}
        aria-describedby={`${id}-hint${error ? ` ${id}-error` : ''}`}
        onChange={(event) => onChange(event.target.value)}
        className={`${TEXT_FIELD} max-w-[12rem] ${invalid ? FIELD_INVALID : ''}`}
      />
      {error ? (
        <p id={`${id}-error`} className={ERROR}>
          {error}
        </p>
      ) : null}
      <p id={`${id}-hint`} className={HINT}>
        {hint}
      </p>
    </div>
  );
}

function DeploymentList({
  section,
  rows,
  fid,
  errors,
  serverError,
  disabled,
  onChange,
}: {
  section: DeploymentSection;
  rows: DeploymentRow[];
  fid: (id: string) => string;
  errors: Record<string, string>;
  serverError?: string;
  disabled: boolean;
  onChange: (rows: DeploymentRow[]) => void;
}) {
  const names = SECTION_NAMES[section];
  const replace = (index: number, row: DeploymentRow) =>
    onChange(rows.map((existing, i) => (i === index ? row : existing)));
  return (
    <div>
      <p className={LABEL}>
        {names.title}
        <span className={VARIABLE}>{section}</span>
      </p>
      {rows.length === 0 ? (
        <p className="text-[12px] text-gray-500">None listed.</p>
      ) : (
        <ul className="divide-y divide-gray-100 rounded-md border border-gray-200">
          {rows.map((row, index) => {
            const endpointId = rowFieldId(section, index, 'endpoint');
            const modelsId = rowFieldId(section, index, 'models');
            const endpointInvalid =
              Boolean(errors[endpointId]) || flaggedBy(serverError, endpointId);
            const modelsInvalid = Boolean(errors[modelsId]) || flaggedBy(serverError, modelsId);
            const kept = Object.keys(row.extra).length;
            return (
              <li
                key={index}
                aria-label={`${names.one} ${index + 1}`}
                className="grid grid-cols-1 gap-2 px-3 py-2 sm:grid-cols-[minmax(0,3fr)_minmax(0,3fr)_auto] sm:items-start"
              >
                <div className="min-w-0">
                  <label htmlFor={fid(endpointId)} className="sr-only">
                    Endpoint of {names.one} {index + 1}
                  </label>
                  <input
                    id={fid(endpointId)}
                    type="text"
                    value={row.endpoint}
                    placeholder="http://10.0.0.5:8000"
                    disabled={disabled}
                    spellCheck={false}
                    autoComplete="off"
                    {...NOT_A_LOGIN_FIELD}
                    aria-invalid={endpointInvalid || undefined}
                    onChange={(event) => replace(index, { ...row, endpoint: event.target.value })}
                    className={`${TEXT_FIELD} font-mono ${endpointInvalid ? FIELD_INVALID : ''}`}
                  />
                  {errors[endpointId] ? <p className={ERROR}>{errors[endpointId]}</p> : null}
                </div>
                <div className="min-w-0">
                  <label htmlFor={fid(modelsId)} className="sr-only">
                    Models of {names.one} {index + 1}
                  </label>
                  <input
                    id={fid(modelsId)}
                    type="text"
                    value={row.models}
                    placeholder="Model IDs, comma-separated"
                    disabled={disabled}
                    spellCheck={false}
                    autoComplete="off"
                    {...NOT_A_LOGIN_FIELD}
                    aria-invalid={modelsInvalid || undefined}
                    onChange={(event) => replace(index, { ...row, models: event.target.value })}
                    className={`${TEXT_FIELD} ${modelsInvalid ? FIELD_INVALID : ''}`}
                  />
                  {errors[modelsId] ? <p className={ERROR}>{errors[modelsId]}</p> : null}
                  {kept > 0 ? (
                    <p className={HINT}>
                      {kept === 1 ? '1 other key is' : `${kept} other keys are`} kept; edit them in
                      JSON.
                    </p>
                  ) : null}
                </div>
                <button
                  type="button"
                  className={BUTTON_GHOST}
                  disabled={disabled}
                  aria-label={`Remove ${names.one} ${index + 1}`}
                  onClick={() => onChange(rows.filter((_, i) => i !== index))}
                >
                  Remove
                </button>
              </li>
            );
          })}
        </ul>
      )}
      <button
        type="button"
        className={`${BUTTON_SECONDARY} mt-2`}
        disabled={disabled}
        onClick={() => onChange([...rows, emptyRow()])}
      >
        Add {names.one}
      </button>
    </div>
  );
}
