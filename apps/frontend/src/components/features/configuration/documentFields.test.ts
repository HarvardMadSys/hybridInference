import { describe, expect, it } from 'vitest';

import {
  documentFields,
  documentText,
  fieldValue,
  keepsItsValues,
  parseDocument,
  pathLabel,
  pathPointer,
  sameJson,
  withFieldTexts,
  type JsonObject,
} from './documentFields';

const CATALOG: JsonObject = {
  models: [
    {
      id: 'example-chat',
      context_length: 4096,
      supports_tools: false,
      supported_params: ['temperature', 'top_p'],
      pricing: { prompt: '0' },
      route: [{ base_url: '${EXAMPLE_UPSTREAM_BASE_URL}', weight: 1.5, region: null }],
    },
  ],
};

/** Each field as `label kind text`, in order. */
function summary(document: JsonObject) {
  return documentFields(document).map((field) => `${field.label} ${field.kind} ${field.text}`);
}

describe('documentFields', () => {
  it('flattens a document into one field per value, in its own order', () => {
    expect(summary(CATALOG)).toEqual([
      'models[0].id string example-chat',
      'models[0].context_length number 4096',
      'models[0].supports_tools boolean false',
      'models[0].supported_params list temperature, top_p',
      'models[0].pricing.prompt string 0',
      'models[0].route[0].base_url string ${EXAMPLE_UPSTREAM_BASE_URL}',
      'models[0].route[0].weight number 1.5',
      'models[0].route[0].region null ',
    ]);
  });

  it('keeps a list as one field only when it reads back unchanged', () => {
    expect(summary({ scopes: ['openid', 'email'] })).toEqual(['scopes list openid, email']);
    // A comma, padding, an empty item or a number would not survive the round trip.
    expect(summary({ stop: ['a,b', ' c'], sizes: [1, 2], empty: [''] })).toEqual([
      'stop[0] string a,b',
      'stop[1] string  c',
      'sizes[0] number 1',
      'sizes[1] number 2',
      'empty[0] string ',
    ]);
  });

  it('shows an empty list or object, which could hold strings or objects alike, but types nothing', () => {
    const document = { local_deployment: [], branding: {}, name: 'x' };
    expect(summary(document)).toEqual([
      'local_deployment empty []',
      'branding empty {}',
      'name string x',
    ]);
    expect(fieldValue('empty', 'anything')).toEqual({ error: 'Add to it in JSON.' });
    expect(withFieldTexts(document, documentFields(document), {}).document).toEqual(document);
  });

  it('names each field by its path, and quotes a key that is no plain name', () => {
    expect(pathLabel(['models', 0, 'route', 1, 'base_url'])).toBe('models[0].route[1].base_url');
    expect(pathLabel(['labels', 'a.b', 'x y'])).toBe('labels["a.b"]["x y"]');
    expect(pathLabel(['provider-id'])).toBe('provider-id');
    expect(pathPointer(['models', 0, 'a/b', 'c~d'])).toBe('/models/0/a~1b/c~0d');
  });
});

describe('fieldValue', () => {
  it('reads a field as the type its value had', () => {
    expect(fieldValue('string', ' 1.0.0 ')).toEqual({ value: ' 1.0.0 ' });
    expect(fieldValue('number', ' 2.5 ')).toEqual({ value: 2.5 });
    expect(fieldValue('number', '-1e3')).toEqual({ value: -1000 });
    expect(fieldValue('boolean', 'TRUE')).toEqual({ value: true });
    expect(fieldValue('boolean', 'false')).toEqual({ value: false });
    expect(fieldValue('list', 'a, b,, c ')).toEqual({ value: ['a', 'b', 'c'] });
    expect(fieldValue('list', '')).toEqual({ value: [] });
  });

  it('says what a number or a switch takes, and accepts nothing else', () => {
    for (const text of ['', 'abc', '0x10', 'Infinity', '1.', '01', '1,5']) {
      expect(fieldValue('number', text)).toEqual({ error: 'Enter a number.' });
    }
    for (const text of ['', 'yes', '1', 'on']) {
      expect(fieldValue('boolean', text)).toEqual({ error: 'Enter true or false.' });
    }
  });

  it('takes what is typed into a value never set as text, unless it is empty or null', () => {
    expect(fieldValue('null', '')).toEqual({ value: null });
    expect(fieldValue('null', ' null ')).toEqual({ value: null });
    // Nothing says it is a number: a year or a version stays text.
    expect(fieldValue('null', '2024')).toEqual({ value: '2024' });
    expect(fieldValue('null', '1.10')).toEqual({ value: '1.10' });
    expect(fieldValue('null', 'true')).toEqual({ value: 'true' });
    expect(fieldValue('null', 'eu-west')).toEqual({ value: 'eu-west' });
  });
});

describe('keepsItsValues', () => {
  it('refuses a document an integer beyond 2^53 would come back from rounded', () => {
    expect(keepsItsValues('{"id": 9007199254740991}')).toBe(true);
    expect(keepsItsValues('{"id": 9007199254740993}')).toBe(false);
    expect(keepsItsValues('{"ids": [1, -9007199254740993]}')).toBe(false);
  });

  it('keeps what a round trip changes only in how it is written', () => {
    // A long decimal and an exponent are doubles for the gateway too.
    expect(keepsItsValues('{"weight": 1.0, "big": 1e300, "p": 0.1000000000000000055}')).toBe(true);
    // Digits in a string are text, escaped quotes included.
    expect(keepsItsValues('{"note": "id 9007199254740993 \\" 12345678901234567890"}')).toBe(true);
    expect(keepsItsValues('')).toBe(true);
  });
});

describe('withFieldTexts', () => {
  const fields = documentFields(CATALOG);
  const id = (label: string) => fields.find((field) => field.label === label)!.id;

  it('puts what was typed into a copy of the document, each value as its type', () => {
    const { document, errors } = withFieldTexts(CATALOG, fields, {
      [id('models[0].context_length')]: '8192',
      [id('models[0].supports_tools')]: 'true',
      [id('models[0].supported_params')]: 'temperature, top_p, stop',
      [id('models[0].route[0].region')]: 'us',
    });

    expect(errors).toEqual({});
    expect(document.models).toEqual([
      {
        ...(CATALOG.models as JsonObject[])[0],
        context_length: 8192,
        supports_tools: true,
        supported_params: ['temperature', 'top_p', 'stop'],
        route: [{ base_url: '${EXAMPLE_UPSTREAM_BASE_URL}', weight: 1.5, region: 'us' }],
      },
    ]);
    // The document itself is left alone.
    expect((CATALOG.models as JsonObject[])[0].context_length).toBe(4096);
  });

  it('leaves a value that does not convert as it was, and says why', () => {
    const { document, errors } = withFieldTexts(CATALOG, fields, {
      [id('models[0].context_length')]: '8k',
    });

    expect(errors).toEqual({ [id('models[0].context_length')]: 'Enter a number.' });
    expect(sameJson(document, CATALOG)).toBe(true);
  });

  it('keeps a key named __proto__ a key of the document', () => {
    const document = JSON.parse('{"__proto__": {"x": "1"}}') as JsonObject;
    const own = documentFields(document);
    const { document: next } = withFieldTexts(document, own, { [own[0].id]: '2' });

    expect(own.map((field) => field.label)).toEqual(['__proto__.x']);
    expect(Object.prototype.hasOwnProperty.call(next, '__proto__')).toBe(true);
    expect(JSON.parse(documentText(next))).toEqual(JSON.parse('{"__proto__": {"x": "2"}}'));
    expect(({} as { x?: string }).x).toBeUndefined();
  });
});

describe('parseDocument and sameJson', () => {
  it('reads an object, blank as the empty document, and nothing else', () => {
    expect(parseDocument('{"a": 1}')).toEqual({ a: 1 });
    expect(parseDocument('  ')).toEqual({});
    expect(parseDocument('[1]')).toBeNull();
    expect(parseDocument('"text"')).toBeNull();
    expect(parseDocument('{"a": ')).toBeNull();
  });

  it('compares values whatever the order of their keys', () => {
    expect(sameJson({ a: 1, b: [1, { c: null }] }, { b: [1, { c: null }], a: 1 })).toBe(true);
    expect(sameJson({ a: 1 }, { a: 1, b: 2 })).toBe(false);
    expect(sameJson([1, 2], [2, 1])).toBe(false);
    expect(sameJson({ a: '1' }, { a: 1 })).toBe(false);
  });

  it('writes a document indented', () => {
    expect(documentText({ a: { b: 1 } })).toBe('{\n  "a": {\n    "b": 1\n  }\n}');
  });
});
