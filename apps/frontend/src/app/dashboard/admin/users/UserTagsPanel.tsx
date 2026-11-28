'use client';

import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  createUserTag,
  deleteUserTag,
  listModelVisibility,
  previewUserTagRule,
  runUserTagRule,
  updateUserTag,
  type UserTag,
  type UserTagPreview,
  type UserTagWrite,
} from '@/lib/api/admin';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import { CHECKBOX, FIELD_HINT, FORM_LABEL } from '@/components/features/admin/adminStyles';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { useMoney } from '@/lib/hooks/useMoney';
import { getErrorMessage } from '@/lib/utils/errors';
import { userAccountLabel } from '@/lib/utils/userLabel';
import { TagChip } from './TagChip';
import { USER_TAGS_QUERY_KEY, useUserTags } from './hooks/useUserTags';
import { USERS_LIST_QUERY_KEY } from './hooks/useUsers';
import { BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SECONDARY, CONTROL, FIELD_LABEL } from './ui';

const TAG_NAME = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const MAX_WINDOW_HOURS = 168;

/** A tag as its editor holds it: every number as typed, `''` for "not set". */
export interface TagDraft {
  name: string;
  description: string;
  quotaDaily: string;
  quotaMonthly: string;
  maxConcurrent: string;
  restrictModels: boolean;
  allowedModels: string[];
  autoEnabled: boolean;
  windowHours: string;
  minActiveHours: string;
}

const amountText = (value: number | string | null) => (value == null ? '' : String(Number(value)));

export function draftOf(tag: UserTag | null): TagDraft {
  return {
    name: tag?.name ?? '',
    description: tag?.description ?? '',
    quotaDaily: amountText(tag?.quota_daily_cost ?? null),
    quotaMonthly: amountText(tag?.quota_monthly_cost ?? null),
    maxConcurrent: tag?.max_concurrent_requests?.toString() ?? '',
    restrictModels: tag?.allowed_models != null,
    allowedModels: [...(tag?.allowed_models ?? [])].sort(),
    autoEnabled: tag?.auto_enabled ?? false,
    windowHours: String(tag?.auto_window_hours ?? 12),
    minActiveHours: String(tag?.auto_min_active_hours ?? 12),
  };
}

const isWhole = (raw: string, min: number, max = Infinity) => {
  const value = Number(raw);
  return raw.trim() !== '' && Number.isInteger(value) && value >= min && value <= max;
};

/** Why a draft cannot be saved, or `null`. */
export function draftError(draft: TagDraft, creating: boolean): string | null {
  if (creating && !TAG_NAME.test(draft.name)) {
    return 'A name is 1–32 lowercase letters, digits, "-" or "_", starting with a letter or digit.';
  }
  for (const [label, raw] of [
    ['daily quota', draft.quotaDaily],
    ['monthly quota', draft.quotaMonthly],
  ] as const) {
    if (raw.trim() !== '' && !(Number.isFinite(Number(raw)) && Number(raw) >= 0)) {
      return `Enter a ${label} of 0 or more, or leave it empty for none.`;
    }
  }
  if (draft.maxConcurrent.trim() !== '' && !isWhole(draft.maxConcurrent, 1, 100_000)) {
    return 'Enter a whole number of concurrent requests from 1, or leave it empty for none.';
  }
  if (draft.restrictModels && draft.allowedModels.length === 0) {
    return 'Choose at least one model, or allow every model.';
  }
  if (!isWhole(draft.windowHours, 1, MAX_WINDOW_HOURS)) {
    return `The rule's window is 1 to ${MAX_WINDOW_HOURS} hours.`;
  }
  if (!isWhole(draft.minActiveHours, 1, Number(draft.windowHours))) {
    return "The rule's active hours are at least 1 and at most its window.";
  }
  return null;
}

const amountOrNull = (raw: string) => (raw.trim() === '' ? null : Number(raw));

/** The write a draft makes: every field when creating, only the changed ones when editing. */
export function draftWrite(
  draft: TagDraft,
  saved: TagDraft | null,
  currency: 'USD' | 'CNY',
): UserTagWrite {
  const write: UserTagWrite = {};
  const changed = (key: keyof TagDraft) =>
    saved === null || JSON.stringify(draft[key]) !== JSON.stringify(saved[key]);
  if (changed('description')) write.description = draft.description.trim();
  if (changed('quotaDaily')) write.quota_daily_cost = amountOrNull(draft.quotaDaily);
  if (changed('quotaMonthly')) write.quota_monthly_cost = amountOrNull(draft.quotaMonthly);
  if (write.quota_daily_cost !== undefined || write.quota_monthly_cost !== undefined) {
    write.currency = currency;
  }
  if (changed('maxConcurrent')) write.max_concurrent_requests = amountOrNull(draft.maxConcurrent);
  if (changed('restrictModels') || changed('allowedModels')) {
    write.allowed_models = draft.restrictModels ? [...draft.allowedModels].sort() : null;
  }
  if (changed('autoEnabled')) write.auto_enabled = draft.autoEnabled;
  if (changed('windowHours')) write.auto_window_hours = Number(draft.windowHours);
  if (changed('minActiveHours')) write.auto_min_active_hours = Number(draft.minActiveHours);
  return write;
}

/** One line on what a tag holds its members to. */
function limitsSummary(tag: UserTag, format: (value: number) => string): string {
  const parts: string[] = [];
  if (tag.quota_daily_cost != null) parts.push(`≤ ${format(Number(tag.quota_daily_cost))}/day`);
  if (tag.quota_monthly_cost != null) {
    parts.push(`≤ ${format(Number(tag.quota_monthly_cost))}/month`);
  }
  if (tag.max_concurrent_requests != null) {
    parts.push(`≤ ${tag.max_concurrent_requests} concurrent`);
  }
  if (tag.allowed_models != null) {
    parts.push(
      tag.allowed_models.length === 1
        ? `only ${tag.allowed_models[0]}`
        : `${tag.allowed_models.length} models only`,
    );
  }
  return parts.length ? parts.join(' · ') : 'No limits';
}

function ruleSummary(tag: UserTag): string {
  if (!tag.auto_enabled) return 'Added by hand only';
  return tag.auto_min_active_hours === tag.auto_window_hours
    ? `Automatic: requests in every one of the last ${tag.auto_window_hours} hours`
    : `Automatic: requests in ${tag.auto_min_active_hours} of the last ${tag.auto_window_hours} hours`;
}

function TagEditor({
  tag,
  onDone,
}: {
  /** The tag edited, or `null` to create one. */
  tag: UserTag | null;
  onDone: () => void;
}) {
  const money = useMoney();
  const toast = useAccountToast();
  const queryClient = useQueryClient();
  const saved = tag ? draftOf(tag) : null;
  const [draft, setDraft] = useState<TagDraft>(() => draftOf(tag));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<UserTagPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const models = useQuery({
    queryKey: ['admin', 'models', 'visibility'],
    queryFn: listModelVisibility,
    staleTime: 60 * 1000,
  });
  const modelIds = [
    ...new Set([...(models.data?.models ?? []).map((m) => m.model_id), ...draft.allowedModels]),
  ].sort();
  const creating = tag === null;
  const invalid = draftError(draft, creating);
  const write = draftWrite(draft, saved, money.currency);
  const unchanged = !creating && Object.keys(write).length === 0;

  const change = (patch: Partial<TagDraft>) => {
    setError(null);
    setDraft((prev) => ({ ...prev, ...patch }));
    if ('windowHours' in patch || 'minActiveHours' in patch) setPreview(null);
  };

  const toggleModel = (modelId: string) =>
    change({
      allowedModels: draft.allowedModels.includes(modelId)
        ? draft.allowedModels.filter((id) => id !== modelId)
        : [...draft.allowedModels, modelId].sort(),
    });

  const save = async () => {
    if (invalid) {
      setError(invalid);
      return;
    }
    if (unchanged) {
      onDone();
      return;
    }
    setSaving(true);
    setError(null);
    try {
      if (creating) await createUserTag({ name: draft.name, ...write });
      else await updateUserTag(tag.name, write);
      toast.success(creating ? `Created tag ${draft.name}` : `Saved tag ${tag.name}`);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: USER_TAGS_QUERY_KEY }),
        queryClient.invalidateQueries({ queryKey: USERS_LIST_QUERY_KEY }),
      ]);
      onDone();
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const runPreview = async () => {
    if (!tag || invalid) {
      if (invalid) setError(invalid);
      return;
    }
    setPreviewing(true);
    try {
      setPreview(
        await previewUserTagRule(tag.name, {
          windowHours: Number(draft.windowHours),
          minActiveHours: Number(draft.minActiveHours),
        }),
      );
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setPreviewing(false);
    }
  };

  return (
    <div
      className="space-y-4 rounded-md border border-gray-200 bg-gray-50/60 p-3"
      data-testid="user-tag-editor"
    >
      <div className="flex flex-wrap items-end gap-3">
        {creating && (
          <label className="block">
            <span className={FORM_LABEL}>Name</span>
            <input
              value={draft.name}
              onChange={(e) => change({ name: e.target.value.trim().toLowerCase() })}
              placeholder="e.g. lab-a"
              aria-label="Tag name"
              className={`${CONTROL} w-40`}
              disabled={saving}
            />
          </label>
        )}
        <label className="block min-w-[16rem] flex-1">
          <span className={FORM_LABEL}>Description</span>
          <input
            value={draft.description}
            onChange={(e) => change({ description: e.target.value })}
            aria-label="Tag description"
            maxLength={500}
            className={`${CONTROL} w-full`}
            disabled={saving}
          />
        </label>
      </div>

      <div>
        <div className={FIELD_LABEL}>Limits for each member</div>
        <p className={FIELD_HINT}>
          Each limit is a ceiling: a member is held to the stricter of its own limit and the
          tag&apos;s. Leave a field empty to set none.
        </p>
        <div className="mt-2 flex flex-wrap items-end gap-3">
          <label className="block">
            <span className={FORM_LABEL}>Daily quota ({money.currency})</span>
            <input
              type="number"
              min={0}
              step="any"
              value={draft.quotaDaily}
              onChange={(e) => change({ quotaDaily: e.target.value })}
              placeholder="None"
              aria-label="Daily quota"
              className={`${CONTROL} w-32`}
              disabled={saving}
            />
          </label>
          <label className="block">
            <span className={FORM_LABEL}>Monthly quota ({money.currency})</span>
            <input
              type="number"
              min={0}
              step="any"
              value={draft.quotaMonthly}
              onChange={(e) => change({ quotaMonthly: e.target.value })}
              placeholder="None"
              aria-label="Monthly quota"
              className={`${CONTROL} w-32`}
              disabled={saving}
            />
          </label>
          <label className="block">
            <span className={FORM_LABEL}>Max concurrent requests</span>
            <input
              type="number"
              min={1}
              step={1}
              value={draft.maxConcurrent}
              onChange={(e) => change({ maxConcurrent: e.target.value })}
              placeholder="None"
              aria-label="Max concurrent requests"
              className={`${CONTROL} w-32`}
              disabled={saving}
            />
          </label>
        </div>
      </div>

      <fieldset className="space-y-2">
        <legend className={FIELD_LABEL}>Models</legend>
        <div className="flex flex-wrap gap-4 text-[13px] text-gray-700">
          <label className="inline-flex items-center gap-2">
            <input
              type="radio"
              name={`models-${tag?.name ?? 'new'}`}
              checked={!draft.restrictModels}
              onChange={() => change({ restrictModels: false })}
              disabled={saving}
            />
            Every model the member may use
          </label>
          <label className="inline-flex items-center gap-2">
            <input
              type="radio"
              name={`models-${tag?.name ?? 'new'}`}
              checked={draft.restrictModels}
              onChange={() => change({ restrictModels: true })}
              disabled={saving}
            />
            Only these models
          </label>
        </div>
        {draft.restrictModels && (
          <div className="grid gap-1.5 sm:grid-cols-2 lg:grid-cols-3" data-testid="tag-models">
            {models.isLoading && <p className={FIELD_HINT}>Loading models…</p>}
            {modelIds.map((modelId) => (
              <label
                key={modelId}
                className="flex cursor-pointer items-center gap-2 rounded-md border border-gray-200 bg-white px-3 py-1.5 text-[12px] text-gray-700"
              >
                <input
                  type="checkbox"
                  className={CHECKBOX}
                  checked={draft.allowedModels.includes(modelId)}
                  onChange={() => toggleModel(modelId)}
                  disabled={saving}
                  aria-label={`Allow ${modelId}`}
                />
                <span className="truncate font-medium">{modelId}</span>
              </label>
            ))}
          </div>
        )}
      </fieldset>

      <fieldset className="space-y-2">
        <legend className={FIELD_LABEL}>Automatic rule</legend>
        <label className="inline-flex items-center gap-2 text-[13px] text-gray-700">
          <input
            type="checkbox"
            className={CHECKBOX}
            checked={draft.autoEnabled}
            onChange={(e) => change({ autoEnabled: e.target.checked })}
            disabled={saving}
          />
          Give this tag automatically to accounts that send requests around the clock
        </label>
        <div className="flex flex-wrap items-center gap-2 text-[13px] text-gray-700">
          <span>Requests in at least</span>
          <input
            type="number"
            min={1}
            max={MAX_WINDOW_HOURS}
            value={draft.minActiveHours}
            onChange={(e) => change({ minActiveHours: e.target.value })}
            aria-label="Active hours"
            className={`${CONTROL} w-20`}
            disabled={saving}
          />
          <span>of the last</span>
          <input
            type="number"
            min={1}
            max={MAX_WINDOW_HOURS}
            value={draft.windowHours}
            onChange={(e) => change({ windowHours: e.target.value })}
            aria-label="Window hours"
            className={`${CONTROL} w-20`}
            disabled={saving}
          />
          <span>hours</span>
          {tag && (
            <button
              type="button"
              className={BUTTON_SECONDARY}
              onClick={() => void runPreview()}
              disabled={saving || previewing}
            >
              {previewing ? 'Checking…' : 'Preview matches'}
            </button>
          )}
        </div>
        <p className={FIELD_HINT}>
          Counted over rolling hours back from now; batch jobs are left out. An automatic tag stays
          until you remove it, and a removed one comes back only for requests sent after.
          {creating ? ' Save the tag to preview who its rule matches.' : ''}
        </p>
        {preview && (
          <div className="rounded-md border border-gray-200 bg-white p-2 text-[12px] text-gray-700">
            <div className="font-medium">
              {preview.matched.length === 0
                ? 'No account matches this rule now.'
                : `${preview.matched.length}${preview.truncated ? '+' : ''} account${
                    preview.matched.length === 1 ? '' : 's'
                  } match now:`}
            </div>
            {preview.matched.length > 0 && (
              <ul className="mt-1 max-h-40 space-y-0.5 overflow-y-auto">
                {preview.matched.map((match) => (
                  <li key={match.user_id} className="flex items-center gap-2">
                    <span className="truncate">{userAccountLabel(match) || match.user_id}</span>
                    <span className="text-gray-400">{match.active_hours} active hours</span>
                    {match.member_source && (
                      <span className="text-gray-400">({match.member_source})</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </fieldset>

      {error && (
        <p role="alert" className="text-[12px] text-red-600">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          className={BUTTON_PRIMARY}
          onClick={() => void save()}
          disabled={saving}
        >
          {saving ? 'Saving…' : creating ? 'Create tag' : 'Save'}
        </button>
        <button type="button" className={BUTTON_SECONDARY} onClick={onDone} disabled={saving}>
          Cancel
        </button>
      </div>
    </div>
  );
}

/**
 * The Users tab's user tags: what each tag holds its members to, how accounts
 * get it, and the way to the members (the table, filtered to the tag).
 */
export function UserTagsPanel({
  activeTag,
  onShowMembers,
}: {
  activeTag: string | null;
  onShowMembers: (tag: string | null) => void;
}) {
  const money = useMoney();
  const toast = useAccountToast();
  const queryClient = useQueryClient();
  const confirmation = useConfirmAction();
  const tagsQuery = useUserTags();
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [running, setRunning] = useState<string | null>(null);
  const tags = tagsQuery.data?.tags ?? [];
  const format = (value: number) => money.format(value, { maximumFractionDigits: 2 });

  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: USER_TAGS_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: USERS_LIST_QUERY_KEY }),
    ]);

  const runRule = async (tag: UserTag) => {
    setRunning(tag.name);
    try {
      const { added } = await runUserTagRule(tag.name);
      toast.success(
        added.length === 0
          ? `No new accounts match ${tag.name}`
          : `Tagged ${added.length} account${added.length === 1 ? '' : 's'} ${tag.name}`,
      );
      await refresh();
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setRunning(null);
    }
  };

  const requestDelete = (tag: UserTag) =>
    confirmation.request({
      title: `Delete the tag ${tag.name}?`,
      body: (
        <p>
          Its {tag.manual_members + tag.auto_members} member
          {tag.manual_members + tag.auto_members === 1 ? '' : 's'} lose the tag and its limits at
          once.
        </p>
      ),
      confirmLabel: 'Delete tag',
      busyLabel: 'Deleting…',
      tone: 'danger',
      run: async () => {
        await deleteUserTag(tag.name);
        if (activeTag === tag.name) onShowMembers(null);
        toast.success(`Deleted tag ${tag.name}`);
        await refresh();
      },
    });

  return (
    <section aria-label="User tags" className="rounded-lg border border-gray-200 bg-white">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2">
        <span className={FIELD_LABEL}>User tags</span>
        {tagsQuery.isError && (
          <span className="text-[12px] text-red-600">Tags could not be loaded.</span>
        )}
        {tags.map((tag) => {
          const members = tag.manual_members + tag.auto_members;
          const active = activeTag === tag.name;
          return (
            <button
              key={tag.name}
              type="button"
              onClick={() => onShowMembers(active ? null : tag.name)}
              aria-pressed={active}
              title={
                active
                  ? 'Show every user'
                  : `Show the ${members} user${members === 1 ? '' : 's'} tagged ${tag.name}`
              }
              className={`rounded-full ${active ? 'ring-2 ring-gray-900' : ''}`}
            >
              <TagChip name={tag.name} title={tag.description || tag.name}>
                <span className="tabular-nums opacity-70">{members}</span>
              </TagChip>
            </button>
          );
        })}
        <button
          type="button"
          className="ml-auto text-[12px] font-medium text-gray-600 hover:text-gray-900"
          onClick={() => setExpanded((open) => !open)}
          aria-expanded={expanded}
        >
          {expanded ? 'Hide tag settings' : 'Manage tags'}
        </button>
      </div>

      {expanded && (
        <div className="space-y-3 border-t border-gray-100 p-3">
          {tags.map((tag) => (
            <div key={tag.name} className="space-y-2" data-testid={`user-tag-${tag.name}`}>
              <div className="flex flex-wrap items-start gap-3">
                <div className="min-w-0 flex-1 space-y-0.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <TagChip name={tag.name} />
                    {tag.system && (
                      <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                        built in
                      </span>
                    )}
                    <span className="text-[12px] text-gray-500">
                      {tag.manual_members + tag.auto_members} member
                      {tag.manual_members + tag.auto_members === 1 ? '' : 's'}
                      {tag.auto_members > 0 ? ` (${tag.auto_members} automatic)` : ''}
                      {tag.dismissed_members > 0 ? ` · ${tag.dismissed_members} dismissed` : ''}
                    </span>
                  </div>
                  {tag.description && (
                    <p className="text-[12px] text-gray-500">{tag.description}</p>
                  )}
                  <p className="text-[12px] text-gray-700">{limitsSummary(tag, format)}</p>
                  {tag.amounts_need_review && (
                    <p role="alert" className="text-[12px] text-red-600">
                      Its quotas were saved in another currency, which this ledger has no rate for,
                      so they allow no spend. Edit the tag and enter both quotas again.
                    </p>
                  )}
                  <p className="text-[12px] text-gray-500">{ruleSummary(tag)}</p>
                </div>
                <div className="flex shrink-0 flex-wrap gap-2">
                  <button
                    type="button"
                    className={BUTTON_SECONDARY}
                    onClick={() => onShowMembers(tag.name)}
                  >
                    Show members
                  </button>
                  {tag.auto_enabled && (
                    <button
                      type="button"
                      className={BUTTON_SECONDARY}
                      onClick={() => void runRule(tag)}
                      disabled={running !== null}
                    >
                      {running === tag.name ? 'Running…' : 'Run rule now'}
                    </button>
                  )}
                  <button
                    type="button"
                    className={BUTTON_SECONDARY}
                    onClick={() => {
                      setCreating(false);
                      setEditing(editing === tag.name ? null : tag.name);
                    }}
                    aria-expanded={editing === tag.name}
                  >
                    Edit
                  </button>
                  {!tag.system && (
                    <button
                      type="button"
                      className={BUTTON_DANGER}
                      onClick={() => requestDelete(tag)}
                      disabled={confirmation.busy}
                    >
                      Delete
                    </button>
                  )}
                </div>
              </div>
              {editing === tag.name && <TagEditor tag={tag} onDone={() => setEditing(null)} />}
            </div>
          ))}
          {creating ? (
            <TagEditor tag={null} onDone={() => setCreating(false)} />
          ) : (
            <button
              type="button"
              className={BUTTON_SECONDARY}
              onClick={() => {
                setEditing(null);
                setCreating(true);
              }}
            >
              New tag
            </button>
          )}
        </div>
      )}
      {confirmation.dialog}
    </section>
  );
}
