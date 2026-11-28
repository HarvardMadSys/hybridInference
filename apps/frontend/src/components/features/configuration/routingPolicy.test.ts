import { describe, expect, it } from 'vitest';

import {
  EMPTY_FORM,
  flaggedBy,
  policyForm,
  policyFromForm,
  policySummary,
  routerOptions,
} from './routingPolicy';

const FULL = {
  default_router: 'routewise',
  timeout: 3,
  health_check: 15,
  routing_parameter: { local_fraction: 0.7 },
  local_deployment: [{ endpoint: 'http://10.0.0.5:8000', models: ['glm-4.6', 'qwen3'] }],
  remote_deployment: [{ endpoint: 'https://api.example.com', models: ['glm-4.6'], note: 'x' }],
  custom: { kept: true },
};

describe('policyForm', () => {
  it('reads blank text as an empty policy', () => {
    expect(policyForm('')).toEqual(EMPTY_FORM);
    expect(policyForm('{}')).toEqual(EMPTY_FORM);
  });

  it('reads every value as text', () => {
    const form = policyForm(JSON.stringify(FULL))!;
    expect(form.defaultRouter).toBe('routewise');
    expect(form.timeout).toBe('3');
    expect(form.healthCheck).toBe('15');
    expect(form.localFraction).toBe('0.7');
    expect(form.local_deployment).toEqual([
      { endpoint: 'http://10.0.0.5:8000', models: 'glm-4.6, qwen3', extra: {} },
    ]);
    expect(form.remote_deployment[0].extra).toEqual({ note: 'x' });
  });

  it.each([
    ['not JSON', '{'],
    ['an array', '[]'],
    ['a router that is not text', '{"default_router": 1}'],
    ['a fractional timeout', '{"timeout": 2.5}'],
    ['a timeout as text', '{"timeout": "2"}'],
    ['a routing_parameter that is not an object', '{"routing_parameter": 1}'],
    ['models that are not text', '{"local_deployment": [{"endpoint": "http://a", "models": [1]}]}'],
    ['a deployment that is not an object', '{"remote_deployment": ["http://a"]}'],
    ['an integer beyond 2^53', '{"timeout": 9007199254740993}'],
  ])('leaves %s to JSON', (_, text) => {
    expect(policyForm(text)).toBeNull();
  });
});

describe('policyFromForm', () => {
  it('writes back exactly the document it read', () => {
    const form = policyForm(JSON.stringify(FULL))!;
    const result = policyFromForm(FULL, form);
    expect(result.errors).toEqual({});
    expect(result.document).toEqual(FULL);
    expect(Object.keys(result.document)).toEqual(Object.keys(FULL));
  });

  it('leaves out what is empty, so the default applies', () => {
    const form = policyForm(JSON.stringify(FULL))!;
    const result = policyFromForm(FULL, {
      ...form,
      defaultRouter: '',
      timeout: '',
      healthCheck: ' ',
      localFraction: '',
      local_deployment: [],
      remote_deployment: [],
    });
    expect(result.document).toEqual({ custom: { kept: true } });
  });

  it('keeps routing_parameter keys it does not edit', () => {
    const base = { routing_parameter: { local_fraction: 0.2, other: 1 } };
    const result = policyFromForm(base, {
      ...policyForm(JSON.stringify(base))!,
      localFraction: '',
    });
    expect(result.document).toEqual({ routing_parameter: { other: 1 } });
  });

  it('names values the backend would refuse', () => {
    const result = policyFromForm(
      {},
      {
        ...EMPTY_FORM,
        timeout: '-1',
        healthCheck: '1.5',
        localFraction: '1.2',
        local_deployment: [{ endpoint: 'ftp://x', models: ' , ', extra: {} }],
        remote_deployment: [{ endpoint: '', models: 'm', extra: {} }],
      },
    );
    expect(result.errors).toEqual({
      timeout: 'Enter 0 or more.',
      health_check: 'Enter a whole number.',
      'routing_parameter/local_fraction': 'Enter a share from 0 to 1.',
      'local_deployment/0/endpoint': 'Start with http:// or https://.',
      'local_deployment/0/models': 'Name at least one model.',
      'remote_deployment/0/endpoint': 'Enter the endpoint URL.',
    });
  });

  it('accepts an endpoint that is a ${NAME} reference', () => {
    const result = policyFromForm(
      {},
      {
        ...EMPTY_FORM,
        local_deployment: [{ endpoint: '${LOCAL_URL}', models: 'a,b', extra: {} }],
      },
    );
    expect(result.errors).toEqual({});
    expect(result.document).toEqual({
      local_deployment: [{ endpoint: '${LOCAL_URL}', models: ['a', 'b'] }],
    });
  });
});

describe('routerOptions', () => {
  it('offers the known routers, and a stored name that is none of them', () => {
    expect(routerOptions('')).toEqual(['fixed', 'routewise']);
    expect(routerOptions('greedy')).toEqual(['fixed', 'routewise', 'greedy']);
  });
});

describe('policySummary', () => {
  it('says what an empty policy does', () => {
    expect(policySummary(EMPTY_FORM)).toEqual([
      'Models that name no router use Fixed.',
      'Health checks are off.',
    ]);
  });

  it('says what a full policy does', () => {
    const form = policyForm(JSON.stringify({ ...FULL, default_router: 'fixed' }))!;
    expect(policySummary(form)).toEqual([
      'Models that name no router use Fixed.',
      "Without a database, Fixed sends 70% of a model's traffic to its local deployments when it also has remote ones. With one, the split is not applied.",
      "Local deployments' /health is checked every 15s and reported on GET /routing; the result does not change where requests go.",
    ]);
  });

  it('says when the split does not apply, and when checks have nothing to check', () => {
    const form = { ...EMPTY_FORM, defaultRouter: 'routewise', healthCheck: '10' };
    expect(
      policySummary({ ...form, remote_deployment: [{ endpoint: 'x', models: 'm', extra: {} }] }),
    ).toEqual([
      'Models that name no router use RouteWise.',
      'The local/remote split applies only when the default router is Fixed.',
      'Health checks are on, but no local deployment is listed to check.',
    ]);
  });
});

describe('flaggedBy', () => {
  it('finds a field, or a value in it, in a refusal', () => {
    expect(flaggedBy('/timeout value must be non-negative', 'timeout')).toBe(true);
    expect(
      flaggedBy('/local_deployment/0/endpoint must start', 'local_deployment/0/endpoint'),
    ).toBe(true);
    expect(flaggedBy('/timeout_x bad', 'timeout')).toBe(false);
    expect(flaggedBy(undefined, 'timeout')).toBe(false);
  });
});
