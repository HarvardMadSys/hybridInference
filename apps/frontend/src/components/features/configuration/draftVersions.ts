import type { ConfigSourceVersion } from '@/lib/api/config';

/**
 * The configuration version each category's unsaved changes were made on, by
 * category id. A save sends it as `If-Match`, so the backend refuses it once
 * anything has been saved since the administrator looked: a draft is never
 * quietly applied on top of changes nobody reviewed.
 *
 * The version is taken when the first change of a category is staged, and only
 * these move it afterwards: this page's own conditional writes (a write that was
 * conditional on version A and committed B proves nothing else happened in
 * between, so a draft made on A may continue from B), and an explicit reload
 * after a conflict, which shows the administrator the newer values. A response
 * that merely reports a newer version, a reload or a save elsewhere never does.
 */
export type DraftVersions = Readonly<Record<string, ConfigSourceVersion>>;

export function sameVersion(
  a: ConfigSourceVersion | null | undefined,
  b: ConfigSourceVersion | null | undefined,
): boolean {
  return Boolean(a && b && a.epoch === b.epoch && a.revision === b.revision);
}

/** Record `shown` as the base of `groupId`'s changes, unless they already have one. */
export function noteEdit(
  versions: DraftVersions,
  groupId: string,
  shown: ConfigSourceVersion | null | undefined,
): DraftVersions {
  if (!shown || groupId in versions) return versions;
  return { ...versions, [groupId]: shown };
}

/** The version a save of `groupId` is conditional on; `null` for an older backend. */
export function saveBase(
  versions: DraftVersions,
  groupId: string,
  shown: ConfigSourceVersion | null | undefined,
): ConfigSourceVersion | null {
  return versions[groupId] ?? shown ?? null;
}

/**
 * After a write conditional on `from` committed `to`: every draft made on `from`
 * continues from `to`, the page's own change. Other bases stay, and conflict.
 */
export function chainVersions(
  versions: DraftVersions,
  from: ConfigSourceVersion | null,
  to: ConfigSourceVersion | null | undefined,
): DraftVersions {
  if (!from || !to || sameVersion(from, to)) return versions;
  let changed = false;
  const next: Record<string, ConfigSourceVersion> = {};
  for (const [groupId, version] of Object.entries(versions)) {
    if (sameVersion(version, from)) {
      next[groupId] = to;
      changed = true;
    } else {
      next[groupId] = version;
    }
  }
  return changed ? next : versions;
}

/** Forget the bases of categories with nothing left unsaved. */
export function withoutVersions(
  versions: DraftVersions,
  groupIds: readonly string[],
): DraftVersions {
  if (!groupIds.some((groupId) => groupId in versions)) return versions;
  const next = { ...versions };
  for (const groupId of groupIds) delete next[groupId];
  return next;
}
