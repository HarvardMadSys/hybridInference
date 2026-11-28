'use client';

import { useId, useMemo, useState, type ReactNode } from 'react';

import {
  BUTTON_DANGER,
  BUTTON_GHOST,
  BUTTON_SECONDARY,
  CHECKBOX,
  CONTROL,
  FIELD_INVALID,
  TEXT_FIELD,
} from '@/components/features/admin/adminStyles';
import type { ConfigEntry } from '@/lib/api/config';

import { currentInput, type ConfigDraft } from './configDrafts';
import {
  isJsonObject,
  parseDocument,
  sameJson,
  type JsonObject,
  type JsonValue,
} from './documentFields';
import {
  ADAPTER_KINDS,
  DEPLOYMENT_TYPES,
  MODEL_TYPES,
  ROUTERS,
  advancedKeys,
  catalogText,
  catalogWithModels,
  duplicateModel,
  listAt,
  modelProblems,
  namedInRefusal,
  newModel,
  newRoute,
  otherRouteKeys,
  parseCatalog,
  parseList,
  parseNumberField,
  routesOf,
  shortTokens,
  stringAt,
  withAdvancedKeys,
  withExplicitRoutes,
  withKey,
} from './modelCatalog';
import { NOT_A_LOGIN_FIELD } from './notALoginField';

type View = 'models' | 'json';

/** With more models than this, a search narrows the list. */
const SEARCH_FROM = 5;

const LABEL = 'mb-1 block text-[12px] font-medium text-gray-700';
const HINT = 'mt-1 text-[11px] text-gray-500';
const SECTION_TITLE = 'mb-2 text-[11px] font-semibold uppercase tracking-wide text-gray-500';
const CHIP = 'rounded bg-gray-100 px-1.5 py-0.5 text-[11px] font-medium text-gray-600';

/** What the editor has made of the catalog, and the draft it last reported. */
interface Session {
  draft: ConfigDraft | undefined;
  saved: string;
  /** A stable key per model, by position, so a card keeps its state as models move. */
  uids: string[];
  /** Field texts that are not a value of their type, by `<uid>:<field>`. */
  errors: Record<string, string>;
  /** Bumped when the catalog changed elsewhere: the buffered fields start again. */
  generation: number;
}

let uidCounter = 0;
const nextUid = () => `m${(uidCounter += 1)}`;

/**
 * The model catalog's editor: each model a card, its fields grouped the way an
 * administrator thinks of them -- what it is called, what it can do, how it is
 * routed, and the endpoints that serve it -- with models and routes added,
 * copied, reordered and removed in place. What the form does not cover stays
 * editable as a model's own JSON, and the whole document is a click away. The
 * draft is the catalog's JSON text, so saving, discarding and conflicts work
 * as they do for any setting.
 */
export function ModelCatalogEditor({
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
  /** The catalog's JSON view: the setting's own text editor. */
  json: ReactNode;
  labelId: string;
  describedBy?: string;
  serverError?: string;
  disabled?: boolean;
}) {
  const text = String(currentInput(entry, draft));
  const catalog = useMemo(() => parseCatalog(text), [text]);
  const [view, setView] = useState<View>(() => (catalog !== null ? 'models' : 'json'));
  const shown: View = view === 'models' && catalog !== null ? 'models' : 'json';

  let note = 'The whole catalog as JSON.';
  if (shown === 'models') note = 'Each model is a card. Changes apply after Save and a restart.';
  else if (parseDocument(text) === null)
    note = 'This text is not a JSON object; correct it to edit models.';
  else if (catalog === null) {
    note =
      'This catalog has a shape (or a number) the model editor cannot keep exactly; edit it as JSON.';
  }

  return (
    <div className="max-w-5xl">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <p className="text-[11px] text-gray-500">{note}</p>
        <div
          role="group"
          aria-label={`Edit ${entry.key} as`}
          className="inline-flex shrink-0 rounded-md border border-gray-200 bg-gray-50 p-0.5 text-[12px]"
        >
          {(['models', 'json'] as const).map((option) => (
            <button
              key={option}
              type="button"
              aria-pressed={shown === option}
              disabled={option === 'models' && catalog === null}
              onClick={() => setView(option)}
              className={`rounded px-2.5 py-0.5 font-medium transition disabled:cursor-not-allowed disabled:opacity-40 ${
                shown === option
                  ? 'bg-white text-gray-900 shadow-sm'
                  : 'text-gray-500 hover:text-gray-900'
              }`}
            >
              {option === 'models' ? 'Models' : 'JSON'}
            </button>
          ))}
        </div>
      </div>
      {shown === 'models' && catalog !== null ? (
        <CatalogForm
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

function CatalogForm({
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
  const listId = useId();
  const savedText = String(currentInput(entry, undefined));
  const text = String(currentInput(entry, draft));
  // The form is only shown for a text that parses.
  const catalog = useMemo(() => parseCatalog(text) ?? { document: {}, models: [] }, [text]);
  const { models } = catalog;

  const [session, setSession] = useState<Session>(() => ({
    draft,
    saved: savedText,
    uids: models.map(nextUid),
    errors: {},
    generation: 0,
  }));
  let current = session;
  // Changed somewhere else -- Discard, the JSON view, a save, a reload: the
  // fields start again from the catalog as it is now.
  if (
    session.draft !== draft ||
    (draft === undefined && session.saved !== savedText) ||
    session.uids.length !== models.length
  ) {
    current = {
      draft,
      saved: savedText,
      uids: session.uids.length === models.length ? session.uids : models.map(nextUid),
      errors: {},
      generation: session.generation + 1,
    };
    setSession(current);
  }

  const [expanded, setExpanded] = useState<Set<string>>(() => {
    const open = new Set<string>();
    current.uids.forEach((uid, index) => {
      const id = stringAt(models[index], 'id');
      if (models.length <= 2 || namedInRefusal(serverError, id)) open.add(uid);
    });
    return open;
  });
  const [search, setSearch] = useState('');
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null);

  const problems = useMemo(() => modelProblems(models), [models]);
  const saved = useMemo(() => parseDocument(savedText), [savedText]);

  /** Report the catalog these models make, with every complaint the form has. */
  const commit = (next: JsonObject[], uids: string[], errors: Record<string, string>) => {
    const document = catalogWithModels(catalog, next);
    const formProblems = Object.values(modelProblems(next)).flat();
    const fieldProblems = Object.values(errors);
    const all = [...formProblems, ...fieldProblems];
    let nextDraft: ConfigDraft | undefined;
    // The saved catalog again is no edit, whatever it already had wrong: only
    // text a field could not read keeps a draft alive.
    if (fieldProblems.length === 0 && saved !== null && sameJson(document, saved)) {
      nextDraft = undefined;
    } else {
      nextDraft = {
        kind: 'value',
        value: catalogText(document),
        ...(all.length > 0
          ? {
              error: all.length === 1 ? all[0] : `${all.length} problems in the catalog: ${all[0]}`,
            }
          : {}),
      };
    }
    setSession({ ...current, draft: nextDraft, uids, errors });
    onChange(entry.key, nextDraft);
  };

  const setModel = (index: number, model: JsonObject) =>
    commit(
      models.map((item, i) => (i === index ? model : item)),
      current.uids,
      current.errors,
    );

  const setFieldError = (key: string, error: string | null) => {
    const errors = { ...current.errors };
    if (error) errors[key] = error;
    else delete errors[key];
    if (errors[key] === current.errors[key]) return;
    commit(models, current.uids, errors);
  };

  /** Errors that belong to a model, dropped when it goes. */
  const errorsWithout = (uid: string) =>
    Object.fromEntries(
      Object.entries(current.errors).filter(([key]) => !key.startsWith(`${uid}:`)),
    );

  const ids = models.map((model) => stringAt(model, 'id'));
  const addModel = () => {
    const uid = nextUid();
    commit([...models, newModel(ids)], [...current.uids, uid], current.errors);
    setExpanded((open) => new Set(open).add(uid));
    setSearch('');
  };
  const duplicate = (index: number) => {
    const uid = nextUid();
    const next = [...models];
    next.splice(index + 1, 0, duplicateModel(models[index], ids));
    const uids = [...current.uids];
    uids.splice(index + 1, 0, uid);
    commit(next, uids, current.errors);
    setExpanded((open) => new Set(open).add(uid));
  };
  const remove = (index: number) => {
    const uid = current.uids[index];
    commit(
      models.filter((_, i) => i !== index),
      current.uids.filter((_, i) => i !== index),
      errorsWithout(uid),
    );
    setConfirmRemove(null);
  };
  const move = (index: number, by: -1 | 1) => {
    const to = index + by;
    if (to < 0 || to >= models.length) return;
    const next = [...models];
    const uids = [...current.uids];
    [next[index], next[to]] = [next[to], next[index]];
    [uids[index], uids[to]] = [uids[to], uids[index]];
    commit(next, uids, current.errors);
  };
  const toggle = (uid: string) =>
    setExpanded((open) => {
      const next = new Set(open);
      if (next.has(uid)) next.delete(uid);
      else next.add(uid);
      return next;
    });

  const needle = search.trim().toLowerCase();
  const matches = (model: JsonObject) =>
    !needle ||
    ['id', 'name', 'provider', 'provider_model_id'].some((key) =>
      stringAt(model, key).toLowerCase().includes(needle),
    ) ||
    listAt(model, 'aliases').toLowerCase().includes(needle);
  const routeCount = models.reduce((sum, model) => sum + (routesOf(model)?.length ?? 1), 0);
  const allOpen = current.uids.length > 0 && current.uids.every((uid) => expanded.has(uid));
  const otherTopLevel = Object.keys(catalog.document).filter((key) => key !== 'models');

  return (
    <fieldset
      disabled={disabled}
      aria-labelledby={labelId}
      aria-describedby={describedBy}
      className="min-w-0 space-y-3"
    >
      <datalist id={`${listId}-kinds`}>
        {ADAPTER_KINDS.map((kind) => (
          <option key={kind} value={kind} />
        ))}
      </datalist>
      <div className="flex flex-wrap items-center gap-2">
        <p className="mr-auto text-[12px] text-gray-600">
          <span className="font-semibold text-gray-900">{models.length}</span>{' '}
          {models.length === 1 ? 'model' : 'models'} ·{' '}
          <span className="font-semibold text-gray-900">{routeCount}</span>{' '}
          {routeCount === 1 ? 'route' : 'routes'}
        </p>
        {models.length > SEARCH_FROM ? (
          <input
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search by id, name, provider or alias"
            aria-label="Search models"
            className={`${CONTROL} w-full sm:w-72`}
          />
        ) : null}
        {models.length > 1 ? (
          <button
            type="button"
            className={BUTTON_GHOST}
            onClick={() => setExpanded(allOpen ? new Set() : new Set(current.uids))}
          >
            {allOpen ? 'Collapse all' : 'Expand all'}
          </button>
        ) : null}
        <button type="button" className={BUTTON_SECONDARY} onClick={addModel}>
          + Add model
        </button>
      </div>

      {models.length === 0 ? (
        <div className="rounded-lg border border-dashed border-gray-300 px-4 py-8 text-center">
          <p className="text-[13px] font-medium text-gray-800">No models yet</p>
          <p className="mt-1 text-[12px] text-gray-500">
            Add a model, name the provider that serves it, and point a route at its endpoint.
          </p>
          <button type="button" className={`${BUTTON_SECONDARY} mt-3`} onClick={addModel}>
            + Add the first model
          </button>
        </div>
      ) : null}

      <ul className="space-y-2">
        {models.map((model, index) => {
          if (!matches(model)) return null;
          const uid = current.uids[index];
          const id = stringAt(model, 'id');
          return (
            <ModelCard
              key={uid}
              uid={uid}
              generation={current.generation}
              index={index}
              count={models.length}
              model={model}
              open={expanded.has(uid)}
              problems={problems[index] ?? []}
              fieldErrors={Object.entries(current.errors)
                .filter(([key]) => key.startsWith(`${uid}:`))
                .map(([, message]) => message)}
              refused={namedInRefusal(serverError, id)}
              kindsListId={`${listId}-kinds`}
              confirmingRemove={confirmRemove === uid}
              onToggle={() => toggle(uid)}
              onChange={(next) => setModel(index, next)}
              onFieldError={(field, error) => setFieldError(`${uid}:${field}`, error)}
              onMove={(by) => move(index, by)}
              onDuplicate={() => duplicate(index)}
              onAskRemove={() => setConfirmRemove(uid)}
              onCancelRemove={() => setConfirmRemove(null)}
              onRemove={() => remove(index)}
            />
          );
        })}
      </ul>
      {needle && !models.some(matches) ? (
        <p className="text-[12px] text-gray-500">No model matches “{search.trim()}”.</p>
      ) : null}
      {otherTopLevel.length > 0 ? (
        <p className="text-[11px] text-gray-500">
          The catalog also has {otherTopLevel.map((key) => `“${key}”`).join(', ')}, kept as they
          are; edit them in JSON.
        </p>
      ) : null}
    </fieldset>
  );
}

function ModelCard({
  uid,
  generation,
  index,
  count,
  model,
  open,
  problems,
  fieldErrors,
  refused,
  kindsListId,
  confirmingRemove,
  onToggle,
  onChange,
  onFieldError,
  onMove,
  onDuplicate,
  onAskRemove,
  onCancelRemove,
  onRemove,
}: {
  uid: string;
  generation: number;
  index: number;
  count: number;
  model: JsonObject;
  open: boolean;
  problems: string[];
  fieldErrors: string[];
  refused: boolean;
  kindsListId: string;
  confirmingRemove: boolean;
  onToggle: () => void;
  onChange: (model: JsonObject) => void;
  onFieldError: (field: string, error: string | null) => void;
  onMove: (by: -1 | 1) => void;
  onDuplicate: () => void;
  onAskRemove: () => void;
  onCancelRemove: () => void;
  onRemove: () => void;
}) {
  const baseId = useId();
  const id = stringAt(model, 'id');
  const name = stringAt(model, 'name');
  const provider = stringAt(model, 'provider');
  const routes = routesOf(model);
  const context = shortTokens(model.context_length);
  const router = stringAt(model, 'router');
  const issues = [...problems, ...fieldErrors];
  const flagged = issues.length > 0 || refused;
  const set = (key: string, value: JsonValue | undefined) => onChange(withKey(model, key, value));
  /** A text field: blank removes the key, so the gateway's default applies. */
  const setText = (key: string, value: string) => set(key, value === '' ? undefined : value);
  const fid = (field: string) => `${baseId}-${field}`;
  const bufferKey = (field: string) => `${generation}-${uid}-${field}`;

  return (
    <li
      data-model-card={id}
      className={`rounded-lg border bg-white ${flagged ? 'border-red-300' : 'border-gray-200'}`}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={`${baseId}-body`}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <span
            aria-hidden
            className={`inline-block text-[10px] text-gray-400 transition-transform ${open ? 'rotate-90' : ''}`}
          >
            ▶
          </span>
          <span className="min-w-0 truncate font-mono text-[13px] font-semibold text-gray-900">
            {id || <span className="italic text-red-600">no id</span>}
          </span>
          {name && name !== id ? (
            <span className="hidden min-w-0 truncate text-[12px] text-gray-500 sm:inline">
              {name}
            </span>
          ) : null}
        </button>
        <div className="flex flex-wrap items-center gap-1.5">
          {provider ? <span className={CHIP}>{provider}</span> : null}
          <span className={CHIP}>
            {routes ? `${routes.length} ${routes.length === 1 ? 'route' : 'routes'}` : '1 endpoint'}
          </span>
          {context ? <span className={CHIP}>{context} ctx</span> : null}
          {model.supports_tools === true ? <span className={CHIP}>tools</span> : null}
          {router ? <span className={CHIP}>{router}</span> : null}
          {stringAt(model, 'model_type') === 'embedding' ? (
            <span className={CHIP}>embedding</span>
          ) : null}
          {flagged ? (
            <span className="rounded bg-red-100 px-1.5 py-0.5 text-[11px] font-medium text-red-700">
              {issues.length > 0 ? `${issues.length} to fix` : 'refused'}
            </span>
          ) : null}
        </div>
      </div>

      {open ? (
        <div id={`${baseId}-body`} className="space-y-5 border-t border-gray-100 px-3 py-4 sm:px-4">
          {issues.length > 0 ? (
            <ul className="list-disc space-y-0.5 rounded-md bg-red-50 py-2 pl-7 pr-3 text-[12px] text-red-700">
              {issues.map((issue) => (
                <li key={issue}>{issue}</li>
              ))}
            </ul>
          ) : null}

          <section>
            <h4 className={SECTION_TITLE}>Identity</h4>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              <TextInput
                id={fid('id')}
                label="Model id"
                required
                mono
                value={id}
                invalid={problems.some((problem) => problem.includes(' id'))}
                hint="What clients send as model."
                onChange={(value) => set('id', value)}
              />
              <TextInput
                id={fid('name')}
                label="Display name"
                value={name}
                placeholder={id || undefined}
                hint="Shown in the console; defaults to the id."
                onChange={(value) => setText('name', value)}
              />
              <TextInput
                id={fid('provider')}
                label="Provider"
                mono
                value={provider}
                list={kindsListId}
                hint="Adapter for routes that name no kind."
                onChange={(value) => setText('provider', value)}
              />
              <TextInput
                id={fid('provider_model_id')}
                label="Upstream model name"
                mono
                value={stringAt(model, 'provider_model_id')}
                placeholder={id || undefined}
                hint="The name the provider knows it by, if different."
                onChange={(value) => setText('provider_model_id', value)}
              />
              <BufferedInput
                key={bufferKey('aliases')}
                id={fid('aliases')}
                label="Aliases"
                mono
                value={model.aliases}
                format={() => listAt(model, 'aliases')}
                parse={(text) => ({ value: parseList(text) })}
                hint="Other names clients may use, comma-separated."
                onValue={(value) => set('aliases', value)}
                onError={(error) => onFieldError('aliases', error)}
              />
              <SelectInput
                id={fid('model_type')}
                label="Type"
                value={stringAt(model, 'model_type')}
                options={MODEL_TYPES}
                defaultLabel="chat (default)"
                onChange={(value) => setText('model_type', value)}
              />
            </div>
          </section>

          <section>
            <h4 className={SECTION_TITLE}>Limits & capabilities</h4>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              <BufferedInput
                key={bufferKey('context_length')}
                id={fid('context_length')}
                label="Context length"
                numeric
                value={model.context_length}
                format={(value) => (value === undefined || value === null ? '' : String(value))}
                parse={(text) => parseNumberField(text, { integer: true, min: 1 })}
                placeholder="8192"
                hint="Tokens. Blank uses 8192."
                onValue={(value) => set('context_length', value)}
                onError={(error) =>
                  onFieldError('context_length', error && `Context length: ${error}`)
                }
              />
              <BufferedInput
                key={bufferKey('max_output_length')}
                id={fid('max_output_length')}
                label="Max output tokens"
                numeric
                value={model.max_output_length}
                format={(value) => (value === undefined || value === null ? '' : String(value))}
                parse={(text) => parseNumberField(text, { integer: true, min: 0 })}
                placeholder="4096"
                hint="0 for an embedding model. Blank uses 4096."
                onValue={(value) => set('max_output_length', value)}
                onError={(error) =>
                  onFieldError('max_output_length', error && `Max output tokens: ${error}`)
                }
              />
              <BufferedInput
                key={bufferKey('input_modalities')}
                id={fid('input_modalities')}
                label="Input modalities"
                mono
                value={model.input_modalities}
                format={() => listAt(model, 'input_modalities')}
                parse={(text) => ({ value: parseList(text) })}
                placeholder="text"
                hint="e.g. text, image. Blank is text only."
                onValue={(value) => set('input_modalities', value)}
                onError={(error) => onFieldError('input_modalities', error)}
              />
              <BufferedInput
                key={bufferKey('reasoning_efforts')}
                id={fid('reasoning_efforts')}
                label="Reasoning efforts"
                mono
                value={model.reasoning_efforts}
                format={() => listAt(model, 'reasoning_efforts')}
                parse={(text) => ({ value: parseList(text) })}
                placeholder="low, medium, high"
                hint="Levels clients may ask for, if any."
                onValue={(value) => set('reasoning_efforts', value)}
                onError={(error) => onFieldError('reasoning_efforts', error)}
              />
              <div className="flex flex-col gap-2 sm:col-span-2 lg:col-span-2 lg:pt-5">
                <Checkbox
                  id={fid('supports_tools')}
                  label="Supports tool calls"
                  checked={model.supports_tools === true}
                  onChange={(on) => set('supports_tools', on ? true : undefined)}
                />
                <Checkbox
                  id={fid('supports_structured_output')}
                  label="Supports structured output (JSON schema)"
                  checked={model.supports_structured_output === true}
                  onChange={(on) => set('supports_structured_output', on ? true : undefined)}
                />
                <Checkbox
                  id={fid('on_demand')}
                  label="On demand: started when first requested"
                  checked={model.on_demand === true}
                  onChange={(on) => set('on_demand', on ? true : undefined)}
                />
              </div>
            </div>
          </section>

          <section>
            <h4 className={SECTION_TITLE}>Routing</h4>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              <SelectInput
                id={fid('router')}
                label="Router"
                value={router}
                options={ROUTERS}
                defaultLabel="Gateway default"
                hint="Its parameters (router_params) are under Advanced."
                onChange={(value) => setText('router', value)}
              />
            </div>
          </section>

          <RoutesSection
            model={model}
            baseId={baseId}
            bufferKey={bufferKey}
            kindsListId={kindsListId}
            onChange={onChange}
            onFieldError={onFieldError}
          />

          <AdvancedSection
            key={bufferKey('advanced')}
            id={fid('advanced')}
            model={model}
            onChange={onChange}
            onError={(error) => onFieldError('advanced', error && `Advanced JSON: ${error}`)}
          />

          <div className="flex flex-wrap items-center gap-2 border-t border-gray-100 pt-3">
            <button
              type="button"
              className={BUTTON_GHOST}
              disabled={index === 0}
              onClick={() => onMove(-1)}
              aria-label={`Move ${id || 'model'} up`}
            >
              ↑ Up
            </button>
            <button
              type="button"
              className={BUTTON_GHOST}
              disabled={index === count - 1}
              onClick={() => onMove(1)}
              aria-label={`Move ${id || 'model'} down`}
            >
              ↓ Down
            </button>
            <button type="button" className={BUTTON_GHOST} onClick={onDuplicate}>
              Duplicate
            </button>
            <div className="ml-auto flex items-center gap-2">
              {confirmingRemove ? (
                <>
                  <span className="text-[12px] text-gray-600">Remove {id || 'this model'}?</span>
                  <button type="button" className={BUTTON_GHOST} onClick={onCancelRemove}>
                    Cancel
                  </button>
                  <button type="button" className={BUTTON_DANGER} onClick={onRemove}>
                    Remove
                  </button>
                </>
              ) : (
                <button type="button" className={BUTTON_DANGER} onClick={onAskRemove}>
                  Remove model
                </button>
              )}
            </div>
          </div>
        </div>
      ) : null}
    </li>
  );
}

function RoutesSection({
  model,
  baseId,
  bufferKey,
  kindsListId,
  onChange,
  onFieldError,
}: {
  model: JsonObject;
  baseId: string;
  bufferKey: (field: string) => string;
  kindsListId: string;
  onChange: (model: JsonObject) => void;
  onFieldError: (field: string, error: string | null) => void;
}) {
  const routes = routesOf(model);
  const provider = stringAt(model, 'provider');

  if (routes === null) {
    // A shorthand model: its one endpoint is on the model itself.
    const set = (key: string, value: string) =>
      onChange(withKey(model, key, value === '' ? undefined : value));
    return (
      <section>
        <div className="mb-2 flex items-center justify-between gap-2">
          <h4 className={SECTION_TITLE}>Endpoint</h4>
          <button
            type="button"
            className={BUTTON_GHOST}
            onClick={() => {
              const explicit = withExplicitRoutes(model);
              onChange(withKey(explicit, 'route', [...(routesOf(explicit) ?? []), newRoute()]));
            }}
          >
            + Add a second route
          </button>
        </div>
        <p className="mb-2 text-[11px] text-gray-500">
          One endpoint, served by the {provider ? <code>{provider}</code> : 'provider'} adapter. Add
          a route to split traffic or add a fallback.
        </p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <TextInput
            id={`${baseId}-base_url`}
            label="Base URL"
            mono
            value={stringAt(model, 'base_url')}
            placeholder="https://api.example.com/v1"
            onChange={(value) => set('base_url', value)}
          />
          <TextInput
            id={`${baseId}-api_key`}
            label="API key"
            mono
            value={stringAt(model, 'api_key')}
            placeholder="${PROVIDER_API_KEY}"
            hint="Reference a secret setting as ${NAME}."
            onChange={(value) => set('api_key', value)}
          />
        </div>
      </section>
    );
  }

  // Without routes, the model is a shorthand again: one endpoint, on the model.
  const setRoutes = (next: JsonObject[]) =>
    onChange(withKey(model, 'route', next.length > 0 ? next : undefined));
  const setRoute = (index: number, route: JsonObject) =>
    setRoutes(routes.map((item, i) => (i === index ? route : item)));
  const totalWeight = routes.reduce((sum, route) => {
    const weight = route.weight === undefined ? 1 : route.weight;
    return sum + (typeof weight === 'number' && weight > 0 ? weight : 0);
  }, 0);

  return (
    <section>
      <div className="mb-2 flex items-center justify-between gap-2">
        <h4 className={SECTION_TITLE}>Routes</h4>
        <button
          type="button"
          className={BUTTON_GHOST}
          onClick={() => setRoutes([...routes, newRoute()])}
        >
          + Add route
        </button>
      </div>
      <ol className="space-y-2">
        {routes.map((route, index) => {
          const rid = `${baseId}-r${index}`;
          const weight = route.weight === undefined ? 1 : route.weight;
          const share =
            typeof weight === 'number' && totalWeight > 0
              ? Math.round((weight / totalWeight) * 100)
              : null;
          const set = (key: string, value: JsonValue | undefined) =>
            setRoute(index, withKey(route, key, value));
          const others = otherRouteKeys(route);
          return (
            <li key={index} className="rounded-md border border-gray-200 bg-gray-50/60 p-3">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <span className="text-[12px] font-semibold text-gray-800">Route {index + 1}</span>
                {share !== null && routes.length > 1 ? (
                  <span className={CHIP}>{share}% of traffic</span>
                ) : null}
                {route.optional === true ? <span className={CHIP}>optional</span> : null}
                <button
                  type="button"
                  className={`${BUTTON_GHOST} ml-auto text-red-600 hover:text-red-700`}
                  onClick={() => setRoutes(routes.filter((_, i) => i !== index))}
                  aria-label={`Remove route ${index + 1}`}
                >
                  Remove
                </button>
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <TextInput
                  id={`${rid}-kind`}
                  label="Adapter kind"
                  mono
                  value={stringAt(route, 'kind')}
                  list={kindsListId}
                  placeholder={provider || 'openai_compat'}
                  onChange={(value) => set('kind', value === '' ? undefined : value)}
                />
                <div className="sm:col-span-1 lg:col-span-2">
                  <TextInput
                    id={`${rid}-base_url`}
                    label="Base URL"
                    mono
                    value={stringAt(route, 'base_url')}
                    placeholder="https://api.example.com/v1"
                    onChange={(value) => set('base_url', value === '' ? undefined : value)}
                  />
                </div>
                <BufferedInput
                  key={bufferKey(`route${index}-weight`)}
                  id={`${rid}-weight`}
                  label="Weight"
                  numeric
                  value={route.weight}
                  format={(value) => (value === undefined || value === null ? '' : String(value))}
                  parse={(text) => parseNumberField(text, { integer: false, min: 0 })}
                  placeholder="1"
                  onValue={(value) => set('weight', value)}
                  onError={(error) =>
                    onFieldError(
                      `route${index}-weight`,
                      error && `Route ${index + 1} weight: ${error}`,
                    )
                  }
                />
                <TextInput
                  id={`${rid}-api_key`}
                  label="API key"
                  mono
                  value={stringAt(route, 'api_key')}
                  placeholder="${PROVIDER_API_KEY}"
                  onChange={(value) => set('api_key', value === '' ? undefined : value)}
                />
                <TextInput
                  id={`${rid}-provider_model_id`}
                  label="Upstream model name"
                  mono
                  value={stringAt(route, 'provider_model_id')}
                  placeholder={
                    stringAt(model, 'provider_model_id') || stringAt(model, 'id') || undefined
                  }
                  onChange={(value) => set('provider_model_id', value === '' ? undefined : value)}
                />
                <SelectInput
                  id={`${rid}-deployment_type`}
                  label="Deployment"
                  value={stringAt(route, 'deployment_type')}
                  options={DEPLOYMENT_TYPES.filter((type) => type !== 'auto')}
                  defaultLabel="auto (from host)"
                  onChange={(value) => set('deployment_type', value === '' ? undefined : value)}
                />
                <div className="flex items-end pb-1.5">
                  <Checkbox
                    id={`${rid}-optional`}
                    label="Optional: skip if its key is unset"
                    checked={route.optional === true}
                    onChange={(on) => set('optional', on ? true : undefined)}
                  />
                </div>
              </div>
              {others.length > 0 ? (
                <p className={HINT}>
                  Also set on this route, kept as is: <code>{others.join(', ')}</code>. Edit them in
                  JSON.
                </p>
              ) : null}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/** The model's keys the form does not cover, as one JSON object. */
function AdvancedSection({
  id,
  model,
  onChange,
  onError,
}: {
  id: string;
  model: JsonObject;
  onChange: (model: JsonObject) => void;
  onError: (error: string | null) => void;
}) {
  const extra = advancedKeys(model);
  const keys = Object.keys(extra);
  const [open, setOpen] = useState(false);
  return (
    <section>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex items-center gap-2 text-left"
      >
        <span
          aria-hidden
          className={`inline-block text-[10px] text-gray-400 transition-transform ${open ? 'rotate-90' : ''}`}
        >
          ▶
        </span>
        <span className={`${SECTION_TITLE} mb-0`}>Advanced</span>
        <span className="text-[11px] text-gray-500">
          {keys.length > 0 ? keys.join(', ') : 'pricing, router_params, extra_body…'}
        </span>
      </button>
      <div hidden={!open} className="mt-2">
        <BufferedInput
          id={id}
          label="Other model settings as JSON"
          mono
          multiline
          value={extra}
          format={(value) =>
            isJsonObject(value as JsonValue) && Object.keys(value as JsonObject).length > 0
              ? JSON.stringify(value, null, 2)
              : ''
          }
          parse={(text) => {
            if (!text.trim()) return { value: {} };
            try {
              const value: unknown = JSON.parse(text);
              return isJsonObject(value) ? { value } : { error: 'Enter a JSON object.' };
            } catch {
              return { error: 'This is not valid JSON yet.' };
            }
          }}
          placeholder={'{\n  "pricing": { "input": "0.5", "output": "1.5" }\n}'}
          hint="Any key the form above does not show: pricing, router_params, extra_body, processor…"
          onValue={(value) => onChange(withAdvancedKeys(model, (value ?? {}) as JsonObject))}
          onError={onError}
        />
      </div>
    </section>
  );
}

function TextInput({
  id,
  label,
  value,
  onChange,
  hint,
  placeholder,
  mono = false,
  required = false,
  invalid = false,
  list,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  hint?: string;
  placeholder?: string;
  mono?: boolean;
  required?: boolean;
  invalid?: boolean;
  list?: string;
}) {
  return (
    <div className="min-w-0">
      <label htmlFor={id} className={LABEL}>
        {label}
        {required ? <span className="text-red-500"> *</span> : null}
      </label>
      <input
        id={id}
        type="text"
        value={value}
        list={list}
        placeholder={placeholder}
        spellCheck={false}
        autoComplete="off"
        {...NOT_A_LOGIN_FIELD}
        aria-invalid={invalid || undefined}
        aria-describedby={hint ? `${id}-hint` : undefined}
        onChange={(event) => onChange(event.target.value)}
        className={`${TEXT_FIELD} ${mono ? 'font-mono' : ''} ${invalid ? FIELD_INVALID : ''}`}
      />
      {hint ? (
        <p id={`${id}-hint`} className={HINT}>
          {hint}
        </p>
      ) : null}
    </div>
  );
}

/**
 * A field whose text is kept as typed -- a number half written, a list being
 * extended -- and reported once it reads as a value. Text that does not is
 * said beside the field and reported as an error, which holds the save back.
 */
function BufferedInput({
  id,
  label,
  value,
  format,
  parse,
  onValue,
  onError,
  hint,
  placeholder,
  mono = false,
  numeric = false,
  multiline = false,
}: {
  id: string;
  label: string;
  value: JsonValue | undefined;
  format: (value: JsonValue | undefined) => string;
  parse: (text: string) => { value: JsonValue | undefined } | { error: string };
  onValue: (value: JsonValue | undefined) => void;
  onError: (error: string | null) => void;
  hint?: string;
  placeholder?: string;
  mono?: boolean;
  numeric?: boolean;
  multiline?: boolean;
}) {
  const [text, setText] = useState(() => format(value));
  const [error, setError] = useState<string | null>(null);
  const [seen, setSeen] = useState(() => JSON.stringify(value ?? null));
  // The value changed from outside this field (another control, an undo):
  // show it, unless what is typed already stands for it.
  const now = JSON.stringify(value ?? null);
  if (now !== seen) {
    setSeen(now);
    const typed = parse(text);
    if ('error' in typed || JSON.stringify(typed.value ?? null) !== now) {
      setText(format(value));
      setError(null);
    }
  }

  const change = (next: string) => {
    setText(next);
    const result = parse(next);
    if ('error' in result) {
      setError(result.error);
      onError(result.error);
      return;
    }
    if (error !== null) {
      setError(null);
      onError(null);
    }
    if (JSON.stringify(result.value ?? null) !== now) {
      setSeen(JSON.stringify(result.value ?? null));
      onValue(result.value);
    }
  };

  const describedBy = [hint ? `${id}-hint` : null, error ? `${id}-error` : null]
    .filter(Boolean)
    .join(' ');
  const shared = {
    id,
    value: text,
    placeholder,
    spellCheck: false,
    autoComplete: 'off',
    ...NOT_A_LOGIN_FIELD,
    'aria-invalid': error ? true : undefined,
    'aria-describedby': describedBy || undefined,
    className: `${TEXT_FIELD} ${mono || multiline ? 'font-mono' : ''} ${error ? FIELD_INVALID : ''}`,
  };
  return (
    <div className="min-w-0">
      <label htmlFor={id} className={LABEL}>
        {label}
      </label>
      {multiline ? (
        <textarea
          {...shared}
          rows={Math.max(4, Math.min(16, text.split('\n').length + 1))}
          onChange={(event) => change(event.target.value)}
        />
      ) : (
        <input
          {...shared}
          type="text"
          inputMode={numeric ? 'decimal' : undefined}
          onChange={(event) => change(event.target.value)}
        />
      )}
      {error ? (
        <p id={`${id}-error`} className="mt-1 text-[11px] text-red-600">
          {error}
        </p>
      ) : hint ? (
        <p id={`${id}-hint`} className={HINT}>
          {hint}
        </p>
      ) : null}
    </div>
  );
}

function SelectInput({
  id,
  label,
  value,
  options,
  defaultLabel,
  hint,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  options: readonly string[];
  defaultLabel: string;
  hint?: string;
  onChange: (value: string) => void;
}) {
  // A value the list does not know is kept, and shown as itself.
  const all = value && !options.includes(value) ? [...options, value] : options;
  return (
    <div className="min-w-0">
      <label htmlFor={id} className={LABEL}>
        {label}
      </label>
      <select
        id={id}
        value={value}
        aria-describedby={hint ? `${id}-hint` : undefined}
        onChange={(event) => onChange(event.target.value)}
        className={`${TEXT_FIELD}`}
      >
        <option value="">{defaultLabel}</option>
        {all.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
      {hint ? (
        <p id={`${id}-hint`} className={HINT}>
          {hint}
        </p>
      ) : null}
    </div>
  );
}

function Checkbox({
  id,
  label,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label htmlFor={id} className="flex items-center gap-2 text-[12px] text-gray-700">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className={CHECKBOX}
      />
      {label}
    </label>
  );
}
