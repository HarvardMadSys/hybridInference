'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { Select } from '@/components/ui/Select';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  ChangeList,
  diffFields,
  plural,
  shown,
} from '@/components/features/admin/confirm/ChangeList';
import { EditBar } from '@/components/features/admin/confirm/EditBar';
import { useConfirmAction } from '@/components/features/admin/confirm/useConfirmAction';
import {
  createSiteUpdate,
  deleteSiteUpdate,
  listSiteUpdates,
  type SiteUpdateInput,
  type SiteUpdateItem,
  type SiteUpdatePlacement,
  updateSiteUpdate,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';
import { AdminEmptyState, AdminLoading, AdminSection, AdminSpinner } from './AdminPrimitives';
import {
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  CHECKBOX,
  FIELD_HINT,
  FORM_LABEL,
  MARKDOWN_PROSE,
  PANEL,
  PILL,
  ROW_ACTION,
  ROW_ACTION_DANGER,
  TEXT_FIELD,
} from './adminStyles';

const EMPTY_FORM: SiteUpdateInput = {
  title: '',
  body: '',
  placement: 'feed',
  published: true,
  link_url: null,
  link_label: null,
};

const PLACEMENT_LABEL: Record<SiteUpdatePlacement, string> = {
  feed: 'Feed',
  banner: 'Banner',
};

/** The same fields as a plain record, which `diffFields` compares. */
type UpdateFields = Pick<SiteUpdateInput, keyof SiteUpdateInput>;

/** The fields of an update, in form order, as they read in a confirmation. */
const FIELDS: ReadonlyArray<{
  key: keyof UpdateFields;
  label: string;
  format?: (value: UpdateFields[keyof UpdateFields]) => string;
}> = [
  { key: 'title', label: 'Title' },
  {
    key: 'placement',
    label: 'Placement',
    format: (value) => PLACEMENT_LABEL[value as SiteUpdatePlacement] ?? shown(value),
  },
  { key: 'body', label: 'Body' },
  { key: 'link_url', label: 'Link URL' },
  { key: 'link_label', label: 'Link label' },
  {
    key: 'published',
    label: 'Visibility',
    format: (value) => (value ? 'Published' : 'Draft'),
  },
];

/** A saved update's fields, as the editor holds them. */
function formOf(u: SiteUpdateItem): SiteUpdateInput {
  return {
    title: u.title,
    body: u.body,
    placement: u.placement,
    published: u.published,
    link_url: u.link_url,
    link_label: u.link_label,
  };
}

function fmtDate(s: string): string {
  return new Date(s).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

function where(placement: SiteUpdatePlacement): string {
  return placement === 'banner' ? 'as the homepage banner' : 'in the homepage Updates section';
}

/**
 * The body as one run of plain text for the list: Markdown marks, link
 * targets and list bullets dropped, list items joined with a middle dot. The
 * full Markdown is one click away.
 */
export function plainExcerpt(markdown: string): string {
  const lines = markdown
    .replace(/```[\s\S]*?```/g, ' ')
    .split('\n')
    .map((line) => {
      const item = /^\s*(?:[-*+]|\d+[.)])\s+/.test(line);
      const text = line
        .replace(/^\s*(?:[-*+]|\d+[.)])\s+/, '')
        .replace(/^\s*(?:#{1,6}|>)\s*/, '')
        .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
        .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
        .replace(/[*_~`]+/g, '')
        .trim();
      return { text, item };
    })
    .filter((line) => line.text !== '');
  return lines
    .map((line, i) => (i === 0 ? '' : line.item && lines[i - 1].item ? ' · ' : ' ') + line.text)
    .join('');
}

function linkHost(url: string): string {
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}

const MD_CLASSES = MARKDOWN_PROSE;

/**
 * Homepage updates. The list is read-only; New update opens a form that is
 * reviewed before it is created, Edit opens one update's fields as drafts that
 * are saved after the administrator confirms `old → new`, and Publish /
 * Unpublish / Delete are each confirmed on their own. Hiding the editor keeps
 * its drafts; opening another update for editing asks before discarding them.
 */
export function SiteUpdatesTab() {
  const toast = useAccountToast();
  const confirmation = useConfirmAction();
  const baseId = useId();
  const [updates, setUpdates] = useState<SiteUpdateItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<SiteUpdateInput>(EMPTY_FORM);
  // The editor opens on demand. Hiding it keeps the draft; only Cancel or a
  // confirmed create or save clears it.
  const [composerOpen, setComposerOpen] = useState(false);
  // Updates whose full Markdown is shown in the list.
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const toggleBody = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  // The opener disappears while the editor is open, so focus moves into the
  // editor when it opens and back to the opener when it is put away. A
  // confirmation returns focus to its trigger as it closes, so the title is
  // focused only once no confirmation is open.
  const [focusRequest, setFocusRequest] = useState(0);
  const focusTitle = useRef(false);
  const returnFocus = useRef(false);
  const openerRef = useRef<HTMLButtonElement>(null);
  const titleRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (focusTitle.current && !confirmation.open) {
      focusTitle.current = false;
      titleRef.current?.focus();
    }
  }, [focusRequest, confirmation.open]);

  useEffect(() => {
    if (!composerOpen && returnFocus.current) {
      returnFocus.current = false;
      openerRef.current?.focus();
    }
  }, [composerOpen]);

  function openComposer() {
    setComposerOpen(true);
    focusTitle.current = true;
    setFocusRequest((n) => n + 1);
  }

  /** Put the editor away, keeping the draft. */
  function hideComposer() {
    returnFocus.current = true;
    setComposerOpen(false);
  }

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listSiteUpdates();
      setUpdates(res.updates);
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    load();
  }, [load]);

  const busy = confirmation.busy;

  /** Clear the draft and close the editor; `refocus` returns to the opener. */
  function resetForm(refocus = false) {
    returnFocus.current = refocus;
    setEditingId(null);
    setForm(EMPTY_FORM);
    setComposerOpen(false);
  }

  function closeEditor() {
    if (busy) return;
    resetForm(true);
  }

  const editing = editingId ? updates.find((u) => u.id === editingId) : undefined;
  // An edit whose update is gone (deleted elsewhere) has nothing left to save
  // to: it counts as no editor at all.
  const editingGone = editingId !== null && editing === undefined;
  const changes = editing ? diffFields<UpdateFields>(formOf(editing), form, FIELDS) : [];
  // Whether the editor holds anything not saved: its fields differ from the
  // update being edited, or from an empty form. Hiding the editor keeps
  // them, so nothing replaces them unasked.
  const saved = editing ? formOf(editing) : EMPTY_FORM;
  const unsaved =
    !editingGone &&
    (Object.keys(EMPTY_FORM) as (keyof SiteUpdateInput)[]).some((key) => form[key] !== saved[key]);

  function switchTo(u: SiteUpdateItem) {
    setEditingId(u.id);
    setForm(formOf(u));
    openComposer();
    if (typeof window !== 'undefined') window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function startEdit(u: SiteUpdateItem) {
    // Edit on the update already in the editor picks it up again.
    if (editingId === u.id && editing) {
      openComposer();
      if (typeof window !== 'undefined') window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }
    if (!unsaved) {
      switchTo(u);
      return;
    }
    const draftOf = editing
      ? `your unsaved changes to “${editing.title}”`
      : 'your unsaved new update';
    confirmation.request({
      title: `Edit “${u.title}”?`,
      confirmLabel: 'Discard and edit',
      cancelLabel: 'Keep my changes',
      tone: 'danger',
      body: <p>{`Opening it discards ${draftOf}. Nothing is saved or deleted on the site.`}</p>,
      run: async () => {
        switchTo(u);
      },
    });
  }

  /** New update, Resume draft or Continue editing: the editor as it was left. */
  function openEditor() {
    if (editingGone) resetForm();
    openComposer();
  }

  function reviewCreate() {
    if (!form.title.trim()) {
      toast.error('Title is required');
      return;
    }
    const input = form;
    confirmation.request({
      title: input.published ? 'Create and publish this update?' : 'Create this update as a draft?',
      confirmLabel: input.published ? 'Create and publish' : 'Create draft',
      busyLabel: 'Creating…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList
            changes={FIELDS.map((field) => ({
              label: field.label,
              after: (field.format ?? ((value) => shown(value as never)))(input[field.key]),
            }))}
          />
          <p>
            {input.published
              ? `It appears ${where(input.placement)} as soon as it is created.`
              : 'Nobody sees it until it is published.'}
            {input.published && input.placement === 'banner'
              ? ' Only the most recent published banner is shown.'
              : ''}
          </p>
        </>
      ),
      run: async () => {
        await createSiteUpdate(input);
        toast.success('Update created');
        resetForm(true);
        await load();
      },
    });
  }

  function reviewEdit() {
    if (!editing || changes.length === 0) return;
    if (!form.title.trim()) {
      toast.error('Title is required');
      return;
    }
    const target = editing;
    const pending = changes;
    // Only the changed fields are sent: what is confirmed is exactly what is written.
    const patch: Partial<SiteUpdateInput> = {};
    for (const field of FIELDS) {
      if (JSON.stringify(target[field.key]) !== JSON.stringify(form[field.key])) {
        Object.assign(patch, { [field.key]: form[field.key] });
      }
    }
    confirmation.request({
      title: `Save ${plural(pending.length, 'change')} to “${target.title}”?`,
      confirmLabel: 'Confirm save',
      busyLabel: 'Saving…',
      cancelLabel: 'Back to editing',
      body: (
        <>
          <ChangeList changes={pending} />
          {target.published || form.published ? (
            <p>The homepage reflects this as soon as it is saved.</p>
          ) : null}
        </>
      ),
      run: async () => {
        await updateSiteUpdate(target.id, patch);
        toast.success('Update saved');
        resetForm(true);
        await load();
      },
    });
  }

  function confirmTogglePublished(u: SiteUpdateItem) {
    const publish = !u.published;
    confirmation.request({
      title: publish ? `Publish “${u.title}”?` : `Unpublish “${u.title}”?`,
      confirmLabel: publish ? 'Publish' : 'Unpublish',
      busyLabel: publish ? 'Publishing…' : 'Unpublishing…',
      body: (
        <p>
          {publish
            ? `It appears ${where(u.placement)} right away.${
                u.placement === 'banner' ? ' Only the most recent published banner is shown.' : ''
              }`
            : `It disappears from the homepage and stays here as a draft.`}
        </p>
      ),
      run: async () => {
        await updateSiteUpdate(u.id, { published: publish });
        await load();
      },
    });
  }

  function confirmRemove(u: SiteUpdateItem) {
    confirmation.request({
      title: `Delete “${u.title}”?`,
      confirmLabel: 'Delete',
      busyLabel: 'Deleting…',
      tone: 'danger',
      body: (
        <p>
          {u.published
            ? `It is removed from the homepage (${PLACEMENT_LABEL[u.placement].toLowerCase()}) and from this list. `
            : 'This draft is removed from this list. '}
          This cannot be undone.
        </p>
      ),
      run: async () => {
        await deleteSiteUpdate(u.id);
        toast.success('Update deleted');
        await load();
      },
    });
  }

  const fieldId = (name: string) => `${baseId}-${name}`;
  const isEditMode = editingId !== null && !editingGone;
  const hasNewDraft = !isEditMode && unsaved;
  const openLabel = isEditMode ? 'Continue editing' : hasNewDraft ? 'Resume draft' : 'New update';
  const composerVisible = composerOpen && !editingGone;

  return (
    <AdminSection
      title="Site updates"
      description="Shown on the homepage: feed entries in its Updates section, and a banner across the top."
      actions={
        <>
          {loading && updates.length > 0 ? <AdminSpinner size="sm" /> : null}
          {!composerVisible && (
            <button type="button" ref={openerRef} onClick={openEditor} className={BUTTON_PRIMARY}>
              {openLabel}
            </button>
          )}
        </>
      }
    >
      {/* Composer. Only its buttons act: Enter in a field submits the form,
          and a title alone must not open a review of a half-written update. */}
      {composerVisible && (
        <form
          aria-labelledby={fieldId('composer-title')}
          onSubmit={(e) => e.preventDefault()}
          className={`${PANEL} animate-admin-reveal mb-4 max-w-5xl`}
        >
          <div className="flex items-start justify-between gap-3 border-b border-gray-100 px-4 py-3">
            <div className="min-w-0">
              <h3
                id={fieldId('composer-title')}
                className="text-[13px] font-semibold text-gray-900"
              >
                {isEditMode ? 'Edit update' : 'New update'}
              </h3>
              {editing ? (
                <p className="mt-0.5 truncate text-[12px] text-gray-500">
                  Editing “{editing.title}”
                </p>
              ) : null}
            </div>
            <button
              type="button"
              onClick={hideComposer}
              disabled={busy}
              title="Hide the editor. Your changes are kept until you cancel or save."
              className={ROW_ACTION}
            >
              Hide
            </button>
          </div>

          {/* Fields beside a preview of the entry where there is room. Every
              track is minmax(0, …) and every item min-w-0, at every width:
              an implicit `auto` track would grow to fit a long word or a wide
              Markdown table in the preview and widen the whole page. */}
          <div className="grid grid-cols-[minmax(0,1fr)] divide-gray-100 lg:grid-cols-[minmax(0,1fr)_20rem] lg:divide-x">
            <div className="min-w-0 space-y-4 p-4">
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
                <div className="min-w-0">
                  <label htmlFor={fieldId('title')} className={FORM_LABEL}>
                    Title
                  </label>
                  <input
                    ref={titleRef}
                    id={fieldId('title')}
                    className={TEXT_FIELD}
                    placeholder="e.g. New model: Llama 4 available"
                    value={form.title}
                    disabled={busy}
                    onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))}
                  />
                </div>
                <div className="min-w-0">
                  <label htmlFor={fieldId('placement')} className={FORM_LABEL}>
                    Placement
                  </label>
                  <Select<SiteUpdatePlacement>
                    id={fieldId('placement')}
                    value={form.placement}
                    disabled={busy}
                    onValueChange={(placement) => setForm((f) => ({ ...f, placement }))}
                    options={[
                      { value: 'feed', label: 'Feed (Updates section)' },
                      { value: 'banner', label: 'Banner (top notice)' },
                    ]}
                    aria-describedby={fieldId('placement-hint')}
                    className="w-full"
                  />
                  <p id={fieldId('placement-hint')} className={FIELD_HINT}>
                    {form.placement === 'banner'
                      ? 'Only the most recent published banner is shown.'
                      : 'Listed in the homepage Updates section, newest first.'}
                  </p>
                </div>
              </div>

              <div>
                <label htmlFor={fieldId('body')} className={FORM_LABEL}>
                  Body (Markdown)
                </label>
                <textarea
                  id={fieldId('body')}
                  rows={5}
                  className={`${TEXT_FIELD} font-mono`}
                  placeholder={
                    'Describe the update. **Markdown** and [links](https://...) are supported.'
                  }
                  value={form.body}
                  disabled={busy}
                  onChange={(e) => setForm((f) => ({ ...f, body: e.target.value }))}
                />
              </div>

              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div className="min-w-0">
                  <label htmlFor={fieldId('link-url')} className={FORM_LABEL}>
                    Link URL (optional)
                  </label>
                  <input
                    id={fieldId('link-url')}
                    className={TEXT_FIELD}
                    placeholder="https://docs.example.com/..."
                    value={form.link_url ?? ''}
                    disabled={busy}
                    onChange={(e) => setForm((f) => ({ ...f, link_url: e.target.value || null }))}
                  />
                </div>
                <div className="min-w-0">
                  <label htmlFor={fieldId('link-label')} className={FORM_LABEL}>
                    Link Label (optional)
                  </label>
                  <input
                    id={fieldId('link-label')}
                    className={TEXT_FIELD}
                    placeholder="e.g. Read more"
                    value={form.link_label ?? ''}
                    disabled={busy}
                    onChange={(e) => setForm((f) => ({ ...f, link_label: e.target.value || null }))}
                  />
                </div>
              </div>
            </div>

            <section
              aria-labelledby={fieldId('preview-title')}
              className="min-w-0 border-t border-gray-100 bg-gray-50/40 p-4 lg:border-t-0"
            >
              <h4 id={fieldId('preview-title')} className={FORM_LABEL}>
                Preview
              </h4>
              {form.title.trim() || form.body.trim() ? (
                <div className="mt-2 min-w-0 rounded-md border border-gray-200 bg-white px-3 py-2.5">
                  <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-gray-400">
                    {form.placement === 'banner' ? 'Banner' : 'Feed entry'}
                  </div>
                  {form.title.trim() ? (
                    <div className="text-[14px] font-medium text-gray-900 [overflow-wrap:anywhere]">
                      {form.title}
                    </div>
                  ) : null}
                  {form.body.trim() ? (
                    <div className={MD_CLASSES}>
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{form.body}</ReactMarkdown>
                    </div>
                  ) : null}
                  {form.link_url ? (
                    <div className="mt-1 text-[12px] font-medium text-blue-600 underline">
                      {form.link_label || 'Learn more'}
                    </div>
                  ) : null}
                </div>
              ) : (
                <p className="mt-2 text-[12px] text-gray-400">
                  The title and body appear here as you type.
                </p>
              )}
            </section>
          </div>

          <div className="border-t border-gray-100 px-4 py-3">
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex items-center gap-2 text-[13px] text-gray-700">
                <input
                  type="checkbox"
                  checked={form.published}
                  disabled={busy}
                  onChange={(e) => setForm((f) => ({ ...f, published: e.target.checked }))}
                  className={CHECKBOX}
                />
                Published (visible on homepage)
              </label>
              {editing ? null : (
                <div className="ml-auto flex items-center gap-2">
                  <button
                    type="button"
                    onClick={closeEditor}
                    disabled={busy}
                    className={BUTTON_SECONDARY}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={reviewCreate}
                    disabled={busy}
                    className={BUTTON_PRIMARY}
                  >
                    Create Update
                  </button>
                </div>
              )}
            </div>
            {editing ? (
              <EditBar
                target={editing.title}
                changeCount={changes.length}
                onCancel={closeEditor}
                onSave={reviewEdit}
                busy={busy}
              />
            ) : null}
          </div>
        </form>
      )}

      {/* List */}
      <div className={PANEL}>
        {loading && updates.length === 0 ? (
          <AdminLoading size="sm" />
        ) : updates.length === 0 ? (
          <AdminEmptyState variant="plain" title="No updates yet." />
        ) : (
          <ul className="divide-y divide-gray-100">
            {updates.map((u) => {
              // The update being edited keeps its Publish and Delete until the
              // editor closes: either would leave its drafts stale or orphaned.
              const isEditing = editing?.id === u.id;
              const isOpen = expanded.has(u.id);
              const excerpt = u.body ? plainExcerpt(u.body) : '';
              const bodyId = fieldId(`body-${u.id}`);
              return (
                <li
                  key={u.id}
                  aria-current={isEditing ? 'true' : undefined}
                  className={`grid grid-cols-[minmax(0,1fr)] gap-x-6 gap-y-2 px-4 py-3.5 transition-colors duration-150 sm:grid-cols-[minmax(0,1fr)_auto] ${
                    isEditing ? 'bg-gray-50' : 'hover:bg-gray-50/60'
                  }`}
                >
                  {/* Title, a two-line excerpt at reading width, then what
                      it is (status, placement), when, and where it links. */}
                  <div className="min-w-0 max-w-[68ch]">
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <h3 className="min-w-0 text-[14px] font-medium leading-5 text-gray-900 [overflow-wrap:anywhere]">
                        {u.title}
                      </h3>
                      {isEditing && (
                        <span className={`${PILL} bg-gray-900 text-white`}>Editing</span>
                      )}
                    </div>
                    {excerpt ? (
                      <p className="mt-0.5 line-clamp-2 text-[13px] leading-5 text-gray-600 [overflow-wrap:anywhere]">
                        {excerpt}
                      </p>
                    ) : null}
                    <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px] text-gray-500">
                      <span
                        className={`${PILL} ${
                          u.published
                            ? 'bg-emerald-50 text-emerald-700'
                            : 'bg-amber-50 text-amber-700'
                        }`}
                      >
                        {u.published ? 'Published' : 'Draft'}
                      </span>
                      <span
                        className={
                          u.placement === 'banner'
                            ? `${PILL} bg-violet-50 text-violet-700`
                            : 'text-gray-500'
                        }
                      >
                        {u.placement === 'banner' ? 'Banner' : 'Feed'}
                      </span>
                      <span aria-hidden="true" className="text-gray-300">
                        ·
                      </span>
                      <time
                        dateTime={u.created_at}
                        title={`Created ${new Date(u.created_at).toLocaleString('en-US')}`}
                      >
                        {fmtDate(u.created_at)}
                      </time>
                      {u.link_url ? (
                        <>
                          <span aria-hidden="true" className="text-gray-300">
                            ·
                          </span>
                          <a
                            href={u.link_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            title={u.link_url}
                            className="max-w-[16rem] truncate rounded text-gray-600 underline decoration-gray-300 underline-offset-2 transition-colors duration-150 hover:text-gray-900 hover:decoration-gray-500"
                          >
                            {u.link_label
                              ? `${u.link_label} (${linkHost(u.link_url)})`
                              : linkHost(u.link_url)}
                          </a>
                        </>
                      ) : null}
                      {u.body ? (
                        <>
                          <span aria-hidden="true" className="text-gray-300">
                            ·
                          </span>
                          <button
                            type="button"
                            onClick={() => toggleBody(u.id)}
                            aria-expanded={isOpen}
                            aria-label={`${isOpen ? 'Hide' : 'Show'} full text of ${u.title}`}
                            aria-controls={isOpen ? bodyId : undefined}
                            className="rounded font-medium text-gray-600 transition-colors duration-150 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-400"
                          >
                            {isOpen ? 'Hide full text' : 'Show full text'}
                          </button>
                        </>
                      ) : null}
                    </div>
                    {isOpen && u.body ? (
                      <div
                        id={bodyId}
                        className={`animate-admin-reveal mt-3 rounded-md border border-gray-200 bg-white px-3 py-2 ${MD_CLASSES}`}
                      >
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{u.body}</ReactMarkdown>
                      </div>
                    ) : null}
                  </div>
                  <div className="-ml-2 flex shrink-0 items-start gap-0.5 sm:ml-0">
                    <button
                      type="button"
                      onClick={() => startEdit(u)}
                      aria-label={`Edit ${u.title}`}
                      className={ROW_ACTION}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() => confirmTogglePublished(u)}
                      disabled={isEditing}
                      aria-label={`${u.published ? 'Unpublish' : 'Publish'} ${u.title}`}
                      className={ROW_ACTION}
                    >
                      {u.published ? 'Unpublish' : 'Publish'}
                    </button>
                    <button
                      type="button"
                      onClick={() => confirmRemove(u)}
                      disabled={isEditing}
                      aria-label={`Delete ${u.title}`}
                      className={ROW_ACTION_DANGER}
                    >
                      Delete
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
      {confirmation.dialog}
    </AdminSection>
  );
}
