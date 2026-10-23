'use client';

import { useCallback, useEffect, useId, useState } from 'react';
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
  BUTTON_DANGER,
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  CHECKBOX,
  FIELD_HINT,
  FORM_LABEL,
  PILL,
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

  function resetForm() {
    setEditingId(null);
    setForm(EMPTY_FORM);
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
      resetForm();
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

  return (
    // Composer and list side by side where there is room, so an edit and the
    // list it changes stay in view together.
    <div className="grid gap-4 xl:grid-cols-[minmax(0,7fr)_minmax(0,5fr)] xl:items-start">
      {/* Composer */}
      <AdminSection title={editingId ? 'Edit Update' : 'New Update'}>
        <div className="space-y-4">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label htmlFor={fieldId('title')} className={FORM_LABEL}>
                Title
              </label>
              <input
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

          <div className="flex flex-wrap items-center gap-3 border-t border-gray-100 pt-4">
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
              {editingId && (
                <button type="button" onClick={resetForm} className={BUTTON_SECONDARY}>
                  Cancel
                </button>
              )}
              <button type="button" onClick={save} disabled={saving} className={BUTTON_PRIMARY}>
                {saving ? 'Saving…' : editingId ? 'Save Changes' : 'Create Update'}
              </button>
            </div>
          </div>
        </div>
      </AdminSection>

      {/* List */}
      <AdminSection
        title="All Updates"
        actions={loading && updates.length > 0 ? <AdminSpinner size="sm" /> : null}
        bodyClassName=""
      >
        {loading && updates.length === 0 ? (
          <AdminLoading size="sm" />
        ) : updates.length === 0 ? (
          <AdminEmptyState variant="plain" title="No updates yet." />
        ) : (
          <ul className="divide-y divide-gray-100">
            {updates.map((u) => (
              <li
                key={u.id}
                className={`px-5 py-4 ${editingId === u.id ? 'bg-gray-50' : ''}`}
                aria-current={editingId === u.id ? 'true' : undefined}
              >
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[14px] font-medium text-gray-900">{u.title}</span>
                      <span
                        className={`${PILL} ${
                          u.placement === 'banner'
                            ? 'bg-purple-50 text-purple-700'
                            : 'bg-gray-100 text-gray-600'
                        }`}
                      >
                        {u.placement}
                      </span>
                      <span
                        className={`${PILL} ${
                          u.published
                            ? 'bg-green-50 text-green-700'
                            : 'bg-yellow-50 text-yellow-700'
                        }`}
                      >
                        {u.published ? 'published' : 'draft'}
                      </span>
                      {editingId === u.id && (
                        <span className={`${PILL} bg-gray-900 text-white`}>editing</span>
                      )}
                    </div>
                    {u.body && (
                      <div className={`mt-1 line-clamp-2 ${MD_CLASSES}`}>
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{u.body}</ReactMarkdown>
                      </div>
                    )}
                    <div className="mt-1 text-[11px] text-gray-400">{fmtDate(u.created_at)}</div>
                  </div>
                  <div className="flex shrink-0 flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={() => togglePublished(u)}
                      className={BUTTON_SECONDARY}
                    >
                      {u.published ? 'Unpublish' : 'Publish'}
                    </button>
                    <button type="button" onClick={() => startEdit(u)} className={BUTTON_SECONDARY}>
                      Edit
                    </button>
                    <button type="button" onClick={() => remove(u)} className={BUTTON_DANGER}>
                      Delete
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </AdminSection>
    </div>
  );
}
