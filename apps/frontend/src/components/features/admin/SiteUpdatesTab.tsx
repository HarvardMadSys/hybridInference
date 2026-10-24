'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
import { Select } from '@/components/ui/Select';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
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

function fmtDate(s: string): string {
  return new Date(s).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
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

export function SiteUpdatesTab() {
  const toast = useAccountToast();
  const baseId = useId();
  const [updates, setUpdates] = useState<SiteUpdateItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<SiteUpdateInput>(EMPTY_FORM);
  // The editor opens on demand. Hiding it keeps the draft; only Cancel, a
  // successful save, or deleting the update being edited clears it.
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
  // editor when it opens and back to the opener when it is put away.
  const [focusRequest, setFocusRequest] = useState(0);
  const returnFocus = useRef(false);
  const openerRef = useRef<HTMLButtonElement>(null);
  const titleRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (focusRequest > 0) titleRef.current?.focus();
  }, [focusRequest]);

  useEffect(() => {
    if (!composerOpen && returnFocus.current) {
      returnFocus.current = false;
      openerRef.current?.focus();
    }
  }, [composerOpen]);

  function openComposer() {
    setComposerOpen(true);
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

  /** Clear the draft and close the editor; `refocus` returns to the opener. */
  function resetForm(refocus = false) {
    returnFocus.current = refocus;
    setEditingId(null);
    setForm(EMPTY_FORM);
    setComposerOpen(false);
  }

  function startEdit(u: SiteUpdateItem) {
    setEditingId(u.id);
    setForm({
      title: u.title,
      body: u.body,
      placement: u.placement,
      published: u.published,
      link_url: u.link_url,
      link_label: u.link_label,
    });
    openComposer();
    if (typeof window !== 'undefined') window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function save() {
    if (!form.title.trim()) {
      toast.error('Title is required');
      return;
    }
    setSaving(true);
    try {
      if (editingId) {
        await updateSiteUpdate(editingId, form);
        toast.success('Update saved');
      } else {
        await createSiteUpdate(form);
        toast.success('Update created');
      }
      resetForm(true);
      await load();
    } catch (err) {
      toast.error(getErrorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  async function togglePublished(u: SiteUpdateItem) {
    try {
      await updateSiteUpdate(u.id, { published: !u.published });
      await load();
    } catch (err) {
      toast.error(getErrorMessage(err));
    }
  }

  async function remove(u: SiteUpdateItem) {
    if (!confirm(`Delete "${u.title}"? This cannot be undone.`)) return;
    try {
      await deleteSiteUpdate(u.id);
      toast.success('Update deleted');
      if (editingId === u.id) resetForm();
      await load();
    } catch (err) {
      toast.error(getErrorMessage(err));
    }
  }

  const fieldId = (name: string) => `${baseId}-${name}`;
  const editingTitle = editingId ? updates.find((u) => u.id === editingId)?.title : undefined;
  const hasNewDraft =
    !editingId &&
    (Object.keys(EMPTY_FORM) as (keyof SiteUpdateInput)[]).some(
      (key) => form[key] !== EMPTY_FORM[key],
    );
  const openLabel = editingId ? 'Continue editing' : hasNewDraft ? 'Resume draft' : 'New update';

  return (
    <AdminSection
      title="Site updates"
      description="Shown on the homepage: feed entries in its Updates section, and a banner across the top."
      actions={
        <>
          {loading && updates.length > 0 ? <AdminSpinner size="sm" /> : null}
          {!composerOpen && (
            <button
              type="button"
              ref={openerRef}
              onClick={openComposer}
              aria-controls={fieldId('composer')}
              aria-expanded={false}
              className={BUTTON_PRIMARY}
            >
              {openLabel}
            </button>
          )}
        </>
      }
    >
      {/* Composer. Only its save button saves: Enter in a field submits the
          form, and a title alone would publish a half-written update. */}
      {composerOpen && (
        <form
          id={fieldId('composer')}
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
                {editingId ? 'Edit update' : 'New update'}
              </h3>
              {editingId ? (
                <p className="mt-0.5 truncate text-[12px] text-gray-500">
                  Editing “{editingTitle ?? form.title}”
                </p>
              ) : null}
            </div>
            <button
              type="button"
              onClick={hideComposer}
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

          <div className="flex flex-wrap items-center gap-3 border-t border-gray-100 px-4 py-3">
            <label className="flex items-center gap-2 text-[13px] text-gray-700">
              <input
                type="checkbox"
                checked={form.published}
                onChange={(e) => setForm((f) => ({ ...f, published: e.target.checked }))}
                className={CHECKBOX}
              />
              Published (visible on homepage)
            </label>
            <div className="ml-auto flex items-center gap-2">
              <button type="button" onClick={() => resetForm(true)} className={BUTTON_SECONDARY}>
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void save()}
                disabled={saving}
                className={BUTTON_PRIMARY}
              >
                {saving ? 'Saving…' : editingId ? 'Save Changes' : 'Create Update'}
              </button>
            </div>
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
              const isEditing = editingId === u.id;
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
                        title={`Created ${new Date(u.created_at).toLocaleString()}`}
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
                      onClick={() => togglePublished(u)}
                      aria-label={`${u.published ? 'Unpublish' : 'Publish'} ${u.title}`}
                      className={ROW_ACTION}
                    >
                      {u.published ? 'Unpublish' : 'Publish'}
                    </button>
                    <button
                      type="button"
                      onClick={() => remove(u)}
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
    </AdminSection>
  );
}
