/**
 * The routing policy (`ROUTING_POLICY`) as a form: what the backend's
 * `RoutingConfig` reads, each value as the text an administrator types.
 *
 * - `default_router`: the router for every model the catalog gives none.
 * - `routing_parameter.local_fraction`: with the Fixed router, the share of a
 *   model's traffic its local deployments get when it also has remote ones.
 * - `local_deployment` / `remote_deployment`: the endpoints that split counts
 *   as local and as remote, each with the models it serves there. The split
 *   is applied once, at startup, and only where no weight-override resolver
 *   is attached -- a gateway without a database; with one, selection reads
 *   the registration weights and the split is inert.
 * - `health_check` / `timeout`: how often the local deployments' `/health` is
 *   checked, in seconds (0 is off), and how long one check waits. The result
 *   is logged and shown on `GET /routing`; it never changes dispatch.
 *
 * A key the form does not edit -- in the document, or in a deployment -- is
 * kept as it is. A document the form cannot show exactly is edited as JSON.
 */

import {
  isJsonObject,
  keepsItsValues,
  parseDocument,
  type JsonObject,
  type JsonValue,
} from './documentFields';
import { ROUTERS, parseList, parseNumberField, withKey } from './modelCatalog';

export const DEPLOYMENT_SECTIONS = ['local_deployment', 'remote_deployment'] as const;
export type DeploymentSection = (typeof DEPLOYMENT_SECTIONS)[number];

/** What the backend uses when the policy leaves a value out. */
export const POLICY_DEFAULTS = {
  default_router: 'fixed',
  timeout: 2,
  health_check: 0,
  local_fraction: 0.5,
} as const;

/** How each router is named in the console. */
export const ROUTER_LABELS: Record<string, string> = { fixed: 'Fixed', routewise: 'RouteWise' };

export function routerLabel(name: string): string {
  return ROUTER_LABELS[name] ?? name;
}

/** The routers to offer: the known ones, and a stored name that is none of them. */
export function routerOptions(current: string): string[] {
  const known: string[] = [...ROUTERS];
  return current && !known.includes(current) ? [...known, current] : known;
}

export interface DeploymentRow {
  endpoint: string;
  /** Comma-separated model IDs. */
  models: string;
  /** The deployment's other keys, kept as they are. */
  extra: JsonObject;
}

export interface PolicyForm {
  /** Empty: the key is left out, and the backend uses Fixed. */
  defaultRouter: string;
  timeout: string;
  healthCheck: string;
  localFraction: string;
  local_deployment: DeploymentRow[];
  remote_deployment: DeploymentRow[];
}

export const EMPTY_FORM: PolicyForm = {
  defaultRouter: '',
  timeout: '',
  healthCheck: '',
  localFraction: '',
  local_deployment: [],
  remote_deployment: [],
};

const numberText = (value: JsonValue | undefined) =>
  typeof value === 'number' ? String(value) : '';

function isWholeNumber(value: JsonValue | undefined): boolean {
  return value === undefined || (typeof value === 'number' && Number.isSafeInteger(value));
}

function rowsOf(value: JsonValue | undefined): DeploymentRow[] | null {
  if (value === undefined) return [];
  if (!Array.isArray(value)) return null;
  const rows: DeploymentRow[] = [];
  for (const item of value) {
    if (!isJsonObject(item)) return null;
    const { endpoint, models, ...extra } = item;
    if (endpoint !== undefined && typeof endpoint !== 'string') return null;
    if (
      models !== undefined &&
      !(Array.isArray(models) && models.every((model) => typeof model === 'string'))
    ) {
      return null;
    }
    rows.push({
      endpoint: endpoint ?? '',
      models: ((models as string[] | undefined) ?? []).join(', '),
      extra,
    });
  }
  return rows;
}

/**
 * The form for a policy's text, or null when the form could not show it
 * exactly: text that is not a JSON object, an integer beyond 2^53, or a value
 * of a type the backend would refuse there. Blank text is an empty policy.
 */
export function policyForm(text: string): PolicyForm | null {
  const document = parseDocument(text);
  if (document === null || !keepsItsValues(text)) return null;
  const { default_router: router, timeout, health_check: healthCheck } = document;
  const parameter = document.routing_parameter;
  if (router !== undefined && typeof router !== 'string') return null;
  if (!isWholeNumber(timeout) || !isWholeNumber(healthCheck)) return null;
  if (parameter !== undefined && !isJsonObject(parameter)) return null;
  const fraction = parameter?.local_fraction;
  if (fraction !== undefined && typeof fraction !== 'number') return null;
  const local = rowsOf(document.local_deployment);
  const remote = rowsOf(document.remote_deployment);
  if (local === null || remote === null) return null;
  return {
    defaultRouter: router ?? '',
    timeout: numberText(timeout),
    healthCheck: numberText(healthCheck),
    localFraction: numberText(fraction),
    local_deployment: local,
    remote_deployment: remote,
  };
}

/** A field's id in `PolicyResult.errors`: its JSON Pointer without the leading slash. */
export function rowFieldId(
  section: DeploymentSection,
  index: number,
  field: 'endpoint' | 'models',
): string {
  return `${section}/${index}/${field}`;
}

export const FIELD_IDS = {
  defaultRouter: 'default_router',
  timeout: 'timeout',
  healthCheck: 'health_check',
  localFraction: 'routing_parameter/local_fraction',
} as const;

export interface PolicyResult {
  document: JsonObject;
  /** Why a field's text is not a value the backend accepts, by field id. */
  errors: Record<string, string>;
}

/** An endpoint the backend accepts: http(s), or a `${NAME}` it expands first. */
function endpointProblem(endpoint: string): string | null {
  const value = endpoint.trim();
  if (!value) return 'Enter the endpoint URL.';
  if (/^https?:\/\//.test(value) || value.includes('${')) return null;
  return 'Start with http:// or https://.';
}

/**
 * The policy the form describes, written onto *base*: every key the form does
 * not edit stays where it was. A field left empty leaves its key out, so the
 * backend's default applies. Fields whose text is not a value of their type
 * are named in `errors`, and keep base's value in the document.
 */
export function policyFromForm(base: JsonObject, form: PolicyForm): PolicyResult {
  const errors: Record<string, string> = {};
  let document = withKey(base, 'default_router', form.defaultRouter.trim() || undefined);

  for (const [key, text] of [
    ['timeout', form.timeout],
    ['health_check', form.healthCheck],
  ] as const) {
    const parsed = parseNumberField(text, { integer: true, min: 0 });
    if ('error' in parsed) errors[key] = parsed.error;
    else document = withKey(document, key, parsed.value);
  }

  const fraction = parseNumberField(form.localFraction, { integer: false, min: 0 });
  if ('error' in fraction) errors[FIELD_IDS.localFraction] = fraction.error;
  else if (fraction.value !== undefined && fraction.value > 1) {
    errors[FIELD_IDS.localFraction] = 'Enter a share from 0 to 1.';
  } else {
    const parameter = isJsonObject(base.routing_parameter) ? base.routing_parameter : {};
    const next = withKey(parameter, 'local_fraction', fraction.value);
    // An empty block is the same as none, and none is not deprecated.
    document = withKey(
      document,
      'routing_parameter',
      Object.keys(next).length > 0 ? next : undefined,
    );
  }

  for (const section of DEPLOYMENT_SECTIONS) {
    const rows = form[section];
    rows.forEach((row, index) => {
      const problem = endpointProblem(row.endpoint);
      if (problem) errors[rowFieldId(section, index, 'endpoint')] = problem;
      if (!parseList(row.models)) {
        errors[rowFieldId(section, index, 'models')] = 'Name at least one model.';
      }
    });
    const value = rows.map((row) => {
      let item: JsonObject = withKey(row.extra, 'endpoint', row.endpoint.trim());
      item = withKey(item, 'models', parseList(row.models) ?? []);
      return item;
    });
    document = withKey(document, section, value.length > 0 ? value : undefined);
  }

  return { document, errors };
}

export function emptyRow(): DeploymentRow {
  return { endpoint: '', models: '', extra: {} };
}

/** One sentence per thing the policy decides, for the top of the card. */
export function policySummary(form: PolicyForm): string[] {
  const lines: string[] = [];
  const router = form.defaultRouter.trim() || POLICY_DEFAULTS.default_router;
  lines.push(`Models that name no router use ${routerLabel(router)}.`);
  const local = form.local_deployment.length;
  const remote = form.remote_deployment.length;
  if (local + remote > 0) {
    const share = Number(form.localFraction.trim() || POLICY_DEFAULTS.local_fraction);
    const percent = Number.isFinite(share) ? `${Math.round(share * 100)}%` : 'a share';
    lines.push(
      router === 'fixed'
        ? `Without a database, Fixed sends ${percent} of a model's traffic to its local deployments when it also has remote ones. With one, the split is not applied.`
        : `The local/remote split applies only when the default router is Fixed.`,
    );
  }
  const interval = Number(form.healthCheck.trim() || POLICY_DEFAULTS.health_check);
  if (Number.isFinite(interval) && interval > 0) {
    lines.push(
      local > 0
        ? `Local deployments' /health is checked every ${interval}s and reported on GET /routing; the result does not change where requests go.`
        : 'Health checks are on, but no local deployment is listed to check.',
    );
  } else {
    lines.push('Health checks are off.');
  }
  return lines;
}

/** Whether the reason a save was refused names the field with this id, or a value in it. */
export function flaggedBy(reason: string | undefined, fieldId: string): boolean {
  if (!reason) return false;
  return reason.includes(`/${fieldId} `) || reason.includes(`/${fieldId}/`);
}
