// The model catalog (`MODEL_CATALOG`) as the structured editor sees it: a
// list of models, each with the fields an administrator usually sets laid out
// as a form, its routes as rows, and every other key kept exactly as it was.
//
// Everything here is pure: it reads a catalog's text and returns new
// documents, so the editor only decides what to show. The gateway still
// validates the whole catalog on save (`check_models_document`); the checks
// here catch what a form can catch before a round trip -- a missing or
// repeated id, a number that is not one.

import {
  documentText,
  isJsonObject,
  keepsItsValues,
  parseDocument,
  type JsonObject,
  type JsonValue,
} from './documentFields';

/** The adapter kinds the gateway ships with, offered as suggestions. */
export const ADAPTER_KINDS = [
  'openai_compat',
  'vllm',
  'sglang',
  'ollama',
  'anthropic',
  'claude',
  'gemini',
  'openrouter',
  'deepseek',
  'zai',
  'kimi',
  'minimax',
  'chutes',
  'featherless',
  'cliproxy',
] as const;

export const DEPLOYMENT_TYPES = ['auto', 'self_hosted', 'provider_api'] as const;
export const ROUTERS = ['fixed', 'routewise'] as const;
export const MODEL_TYPES = ['chat', 'embedding'] as const;

/** Model keys the form edits. Any other key is the model's "advanced" JSON. */
export const FORM_MODEL_KEYS = [
  'id',
  'name',
  'provider',
  'provider_model_id',
  'aliases',
  'model_type',
  'context_length',
  'max_output_length',
  'supports_tools',
  'supports_structured_output',
  'input_modalities',
  'reasoning_efforts',
  'on_demand',
  'router',
  'base_url',
  'api_key',
  'route',
] as const;

/** Route keys the route rows edit. Any other key is kept, and shown as kept. */
export const FORM_ROUTE_KEYS = [
  'kind',
  'base_url',
  'provider_model_id',
  'api_key',
  'weight',
  'deployment_type',
  'optional',
] as const;

const FORM_MODEL_KEY_SET = new Set<string>(FORM_MODEL_KEYS);
const FORM_ROUTE_KEY_SET = new Set<string>(FORM_ROUTE_KEYS);

export interface Catalog {
  /** The whole document: `models`, and any other top-level key, kept as is. */
  document: JsonObject;
  models: JsonObject[];
}

/**
 * The catalog `text` holds, or `null` when the structured editor cannot show
 * it without losing something: not a JSON object, `models` not a list of
 * objects, or an integer JavaScript would round.
 */
export function parseCatalog(text: string): Catalog | null {
  const document = parseDocument(text);
  if (document === null || !keepsItsValues(text)) return null;
  const models = document.models ?? [];
  if (!Array.isArray(models) || !models.every(isJsonObject)) return null;
  return { document, models: models as JsonObject[] };
}

/** The document with `models` replaced; blank when the catalog had no `models` and still has none. */
export function catalogWithModels(catalog: Catalog, models: JsonObject[]): JsonObject {
  return { ...catalog.document, models };
}

export function catalogText(document: JsonObject): string {
  return documentText(document);
}

/** A copy of `object` with `key` set, or removed when `value` is `undefined`. Key order is kept. */
export function withKey(object: JsonObject, key: string, value: JsonValue | undefined): JsonObject {
  const next: JsonObject = {};
  let placed = false;
  for (const [existing, item] of Object.entries(object)) {
    if (existing === key) {
      placed = true;
      if (value !== undefined) define(next, key, value);
    } else {
      define(next, existing, item);
    }
  }
  if (!placed && value !== undefined) define(next, key, value);
  return next;
}

/** Defined rather than assigned: a key named `__proto__` stays a key. */
function define(object: JsonObject, key: string, value: JsonValue) {
  Object.defineProperty(object, key, {
    value,
    writable: true,
    enumerable: true,
    configurable: true,
  });
}

export function stringAt(object: JsonObject, key: string): string {
  const value = object[key];
  if (value === undefined || value === null) return '';
  return typeof value === 'string' ? value : JSON.stringify(value);
}

/** A list of strings as one comma-separated text; anything else as its JSON. */
export function listAt(object: JsonObject, key: string): string {
  const value = object[key];
  if (value === undefined || value === null) return '';
  if (Array.isArray(value) && value.every((item) => typeof item === 'string')) {
    return value.join(', ');
  }
  return JSON.stringify(value);
}

export function parseList(text: string): string[] | undefined {
  const items = text
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
  return items.length > 0 ? items : undefined;
}

/**
 * What a number field's text stands for: `undefined` for blank (the key is
 * removed, so the gateway's default applies), the number, or why it is not one.
 */
export function parseNumberField(
  text: string,
  { integer, min }: { integer: boolean; min: number },
): { value: number | undefined } | { error: string } {
  const trimmed = text.trim();
  if (trimmed === '') return { value: undefined };
  const pattern = integer ? /^-?\d+$/ : /^-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;
  const value = Number(trimmed.replace(/_/g, ''));
  if (!pattern.test(trimmed.replace(/_/g, '')) || !Number.isFinite(value)) {
    return { error: integer ? 'Enter a whole number.' : 'Enter a number.' };
  }
  if (value < min) return { error: `Enter ${min} or more.` };
  return { value };
}

/** The model's keys the form does not edit, as their own object. */
export function advancedKeys(model: JsonObject): JsonObject {
  const extra: JsonObject = {};
  for (const [key, value] of Object.entries(model)) {
    if (!FORM_MODEL_KEY_SET.has(key)) define(extra, key, value);
  }
  return extra;
}

/** The model with its advanced keys replaced by `extra`; form keys come first, as they were. */
export function withAdvancedKeys(model: JsonObject, extra: JsonObject): JsonObject {
  const next: JsonObject = {};
  for (const [key, value] of Object.entries(model)) {
    if (FORM_MODEL_KEY_SET.has(key)) define(next, key, value);
  }
  for (const [key, value] of Object.entries(extra)) {
    // A form key typed into the advanced JSON belongs to the form; it is set there.
    define(next, key, value);
  }
  return next;
}

/** A route's keys the row does not edit, by name. */
export function otherRouteKeys(route: JsonObject): string[] {
  return Object.keys(route).filter((key) => !FORM_ROUTE_KEY_SET.has(key));
}

export function routesOf(model: JsonObject): JsonObject[] | null {
  const routes = model.route;
  if (!Array.isArray(routes) || routes.length === 0) return null;
  return routes.filter(isJsonObject) as JsonObject[];
}

/**
 * A model written as a shorthand -- no route list, its endpoint on the model --
 * turned into one with routes: the endpoint becomes route 1, served by the
 * adapter the model's provider names, which is what the gateway synthesizes.
 */
export function withExplicitRoutes(model: JsonObject): JsonObject {
  if (routesOf(model) !== null) return model;
  let first: JsonObject = { weight: 1 };
  for (const key of ['base_url', 'api_key'] as const) {
    if (model[key] !== undefined && model[key] !== null) first = withKey(first, key, model[key]);
  }
  let next = withKey(withKey(model, 'base_url', undefined), 'api_key', undefined);
  next = withKey(next, 'route', [first]);
  return next;
}

export function newRoute(): JsonObject {
  return { kind: 'openai_compat', base_url: '', weight: 1 };
}

/** A new model: an id not yet taken, and one route to fill in. */
export function newModel(taken: readonly string[]): JsonObject {
  let id = 'new-model';
  for (let n = 2; taken.includes(id); n += 1) id = `new-model-${n}`;
  return { id, name: '', provider: '', context_length: 8192, route: [newRoute()] };
}

/** A copy of `model` under an id not yet taken. */
export function duplicateModel(model: JsonObject, taken: readonly string[]): JsonObject {
  const base = `${stringAt(model, 'id') || 'model'}-copy`;
  let id = base;
  for (let n = 2; taken.includes(id); n += 1) id = `${base}-${n}`;
  const copy = JSON.parse(JSON.stringify(model)) as JsonObject;
  // An alias belongs to one model: the copy claiming it too is refused.
  return withKey(withKey(copy, 'id', id), 'aliases', undefined);
}

/** What is wrong with each model that the form can tell, by position. */
export function modelProblems(models: readonly JsonObject[]): Record<number, string[]> {
  const problems: Record<number, string[]> = {};
  const add = (index: number, problem: string) => {
    (problems[index] ??= []).push(problem);
  };
  const seen = new Map<string, number>();
  const names = new Map<string, string>();
  models.forEach((model, index) => {
    const id = typeof model.id === 'string' ? model.id.trim() : '';
    if (!id) {
      add(index, 'Give the model an id.');
    } else if (seen.has(id)) {
      add(index, `Another model already has the id "${id}".`);
    } else {
      seen.set(id, index);
    }
    const aliases = Array.isArray(model.aliases) ? model.aliases : [];
    for (const alias of aliases) {
      if (typeof alias !== 'string') continue;
      const owner = names.get(alias);
      if (owner !== undefined && owner !== id) {
        add(index, `The alias "${alias}" is already ${owner}'s.`);
      }
      names.set(alias, id);
    }
    const routes = routesOf(model);
    if (routes === null) {
      if (!stringAt(model, 'provider')) {
        add(index, 'Name a provider, or add a route with an adapter kind.');
      }
    } else {
      routes.forEach((route, number) => {
        if (!stringAt(route, 'kind') && !stringAt(model, 'provider')) {
          add(index, `Route ${number + 1} needs an adapter kind (or the model a provider).`);
        }
      });
    }
  });
  return problems;
}

/** A short count: 8192 → "8K", 1048576 → "1M". */
export function shortTokens(value: JsonValue | undefined): string | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  if (value >= 1_000_000 && value % 1024 !== 0) return `${+(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_048_576) return `${+(value / 1_048_576).toFixed(1)}M`;
  if (value >= 1000 && value % 1024 === 0) return `${value / 1024}K`;
  if (value >= 1000) return `${+(value / 1000).toFixed(1)}K`;
  return String(value);
}

/** Whether a refusal from the gateway names this model: its messages say `(model 'id'…)`. */
export function namedInRefusal(reason: string | undefined, id: string): boolean {
  if (!reason || !id) return false;
  return reason.includes(`model '${id}'`) || reason.includes(`model "${id}"`);
}
