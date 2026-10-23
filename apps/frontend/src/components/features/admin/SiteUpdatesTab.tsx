'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
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

const MD_CLASSES =
  'text-[13px] text-gray-700 [&_a]:text-blue-600 [&_a]:underline [&_h1]:text-lg [&_h1]:font-semibold [&_h2]:text-base [&_h2]:font-semibold [&_p]:my-2 [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5';

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
      {/* Composer */}
      {composerOpen && (
        <form
          id={fieldId('composer')}
          aria-labelledby={fieldId('composer-title')}
          onSubmit={(e) => {
            e.preventDefault();
            if (!saving) void save();
          }}
          className={`${PANEL} mb-4 max-w-4xl`}
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

          <div className="space-y-4 p-4">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
              <div>
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
              <div>
                <label htmlFor={fieldId('placement')} className={FORM_LABEL}>
                  Placement
                </label>
                <select
                  id={fieldId('placement')}
                  value={form.placement}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, placement: e.target.value as SiteUpdatePlacement }))
                  }
                  aria-describedby={fieldId('placement-hint')}
                  className={TEXT_FIELD}
                >
                  <option value="feed">Feed (Updates section)</option>
                  <option value="banner">Banner (top notice)</option>
                </select>
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
              {form.body && (
                <div className="mt-2">
                  <div className={FORM_LABEL}>Preview</div>
                  <div
                    className={`rounded-md border border-gray-200 bg-gray-50/50 px-3 py-2 ${MD_CLASSES}`}
                  >
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{form.body}</ReactMarkdown>
                  </div>
                </div>
              )}
            </div>

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div>
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
              <div>
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
              <button type="submit" disabled={saving} className={BUTTON_PRIMARY}>
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
              return (
                <li
                  key={u.id}
                  aria-current={isEditing ? 'true' : undefined}
                  className={`flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-start sm:gap-6 ${
                    isEditing ? 'bg-gray-50' : ''
                  }`}
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <h3 className="text-[14px] font-medium text-gray-900">{u.title}</h3>
                      {isEditing && (
                        <span className={`${PILL} bg-gray-900 text-white`}>Editing</span>
                      )}
                    </div>
                    {u.body && (
                      <div className={`mt-1 line-clamp-2 max-w-3xl ${MD_CLASSES} text-gray-600`}>
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{u.body}</ReactMarkdown>
                      </div>
                    )}
                    <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-gray-500">
                      <span className="inline-flex items-center gap-1.5">
                        <span
                          aria-hidden="true"
                          className={`h-1.5 w-1.5 rounded-full ${
                            u.published ? 'bg-emerald-500' : 'bg-amber-500'
                          }`}
                        />
                        <span className={u.published ? 'text-gray-700' : 'text-amber-700'}>
                          {u.published ? 'Published' : 'Draft'}
                        </span>
                      </span>
                      <span className={u.placement === 'banner' ? 'text-purple-700' : undefined}>
                        {u.placement === 'banner' ? 'Banner' : 'Feed'}
                      </span>
                      <span>{fmtDate(u.created_at)}</span>
                    </div>
                  </div>
                  <div className="-ml-2 flex shrink-0 items-center gap-0.5 sm:ml-0">
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
