import { describe, expect, it } from 'vitest';

import { chainVersions, noteEdit, saveBase, withoutVersions } from './draftVersions';

const v = (revision: number, epoch = 'e1') => ({ epoch, revision });

describe('draft versions', () => {
  it('keeps the version a category’s first change was made on', () => {
    let versions = noteEdit({}, 'email', v(41));
    versions = noteEdit(versions, 'email', v(45));
    expect(versions).toEqual({ email: v(41) });
    // A later read shows 45; the draft's save is still conditional on 41.
    expect(saveBase(versions, 'email', v(45))).toEqual(v(41));
    // A category with nothing staged saves on what is shown.
    expect(saveBase(versions, 'general', v(45))).toEqual(v(45));
    // An older backend reports no version: nothing is conditional.
    expect(noteEdit({}, 'email', null)).toEqual({});
    expect(saveBase({}, 'email', null)).toBeNull();
  });

  it('moves only drafts made on the version this page’s own write was conditional on', () => {
    const versions = { email: v(41), general: v(41), network: v(40), other: v(41, 'e0') };
    expect(chainVersions(versions, v(41), v(42))).toEqual({
      email: v(42),
      general: v(42),
      network: v(40),
      other: v(41, 'e0'),
    });
    // An unconditional write, or one that committed nothing, moves nothing.
    expect(chainVersions(versions, null, v(42))).toBe(versions);
    expect(chainVersions(versions, v(41), null)).toBe(versions);
  });

  it('forgets a category once nothing of it is unsaved', () => {
    expect(withoutVersions({ email: v(41), general: v(41) }, ['email'])).toEqual({
      general: v(41),
    });
  });
});
