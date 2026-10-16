import { describe, expect, it } from 'vitest';

import { describeErrorBody } from '../detail';

/** The gateway's 422 body for `message`. */
function gateway(message: string) {
  return { error: { type: 'validation_error', message, code: 422 } };
}

describe('describeErrorBody', () => {
  it("reads the fields of the gateway's validation error, which has no reasons", () => {
    expect(describeErrorBody(gateway('Invalid request: body.password'))).toEqual({
      message: 'Invalid request: body.password',
      fields: { password: '' },
    });
    expect(
      describeErrorBody(gateway('Invalid request: body.login_name, body.setup_code')).fields,
    ).toEqual({ login_name: '', setup_code: '' });
  });

  it('names the innermost field of a nested path, as for a FastAPI detail list', () => {
    expect(
      describeErrorBody(
        gateway('Invalid request: body.items.0.name, body.items.1.name, query.limit'),
      ).fields,
    ).toEqual({ name: '', limit: '' });
  });

  it('names no field for an error about the whole body', () => {
    expect(describeErrorBody(gateway('Invalid request: body')).fields).toEqual({});
    expect(describeErrorBody(gateway('Invalid request')).fields).toEqual({});
  });

  it('reads nothing into a sentence that only starts like one', () => {
    expect(describeErrorBody({ error: { message: 'Invalid request: model is required' } })).toEqual(
      { message: 'Invalid request: model is required', fields: {} },
    );
  });

  it("keeps FastAPI's own reasons, without pydantic's prefix", () => {
    expect(
      describeErrorBody({
        detail: [
          { loc: ['body', 'password'], msg: 'Value error, Password must contain a number' },
          { loc: ['body', 'login_name'], msg: 'String should match pattern' },
        ],
      }),
    ).toEqual({
      message: 'password: Password must contain a number; login_name: String should match pattern',
      fields: {
        password: 'Password must contain a number',
        login_name: 'String should match pattern',
      },
    });
  });
});
