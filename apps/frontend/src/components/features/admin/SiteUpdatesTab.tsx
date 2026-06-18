'use client';

import { useCallback, useEffect, useState } from 'react';
import toast from 'react-hot-toast';
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
  }, []);

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

  return (
    <div className="mt-8 space-y-6">
      {/* Composer */}
      <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
        <h2 className="mb-4 text-[15px] font-semibold text-gray-900">
          {editingId ? 'Edit Update' : 'New Update'}
        </h2>

        <div className="mb-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label className="mb-1 block text-[12px] font-medium text-gray-600">Title</label>
            <input
              className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
              placeholder="e.g. New model: Llama 4 available"
              value={form.title}
              onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))}
            />
          </div>
          <div>
            <label className="mb-1 block text-[12px] font-medium text-gray-600">Placement</label>
            <select
              value={form.placement}
              onChange={(e) =>
                setForm((f) => ({ ...f, placement: e.target.value as SiteUpdatePlacement }))
              }
              className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
            >
              <option value="feed">Feed (Updates section)</option>
              <option value="banner">Banner (top notice)</option>
            </select>
            <p className="mt-1 text-[11px] text-gray-400">
              {form.placement === 'banner'
                ? 'Only the most recent published banner is shown.'
                : 'Listed in the homepage Updates section, newest first.'}
            </p>
          </div>
        </div>

        <div className="mb-4">
          <label className="mb-1 block text-[12px] font-medium text-gray-600">
            Body (Markdown)
          </label>
          <textarea
            rows={5}
            className="w-full rounded-md border border-gray-200 px-3 py-2 font-mono text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
            placeholder={
              'Describe the update. **Markdown** and [links](https://...) are supported.'
            }
            value={form.body}
            onChange={(e) => setForm((f) => ({ ...f, body: e.target.value }))}
          />
          {form.body && (
            <div className="mt-2">
              <label className="mb-1 block text-[12px] font-medium text-gray-600">Preview</label>
              <div className={`rounded-md border border-gray-200 px-3 py-2 ${MD_CLASSES}`}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{form.body}</ReactMarkdown>
              </div>
            </div>
          )}
        </div>

        <div className="mb-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label className="mb-1 block text-[12px] font-medium text-gray-600">
              Link URL (optional)
            </label>
            <input
              className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
              placeholder="https://doc.freeinference.org/..."
              value={form.link_url ?? ''}
              onChange={(e) => setForm((f) => ({ ...f, link_url: e.target.value || null }))}
            />
          </div>
          <div>
            <label className="mb-1 block text-[12px] font-medium text-gray-600">
              Link Label (optional)
            </label>
            <input
              className="w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900"
              placeholder="e.g. Read more"
              value={form.link_label ?? ''}
              onChange={(e) => setForm((f) => ({ ...f, link_label: e.target.value || null }))}
            />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-4">
          <label className="flex items-center gap-2 text-[13px] text-gray-700">
            <input
              type="checkbox"
              checked={form.published}
              onChange={(e) => setForm((f) => ({ ...f, published: e.target.checked }))}
            />
            Published (visible on homepage)
          </label>
          <div className="ml-auto flex items-center gap-3">
            {editingId && (
              <button
                onClick={resetForm}
                className="rounded-md border border-gray-300 px-4 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50"
              >
                Cancel
              </button>
            )}
            <button
              onClick={save}
              disabled={saving}
              className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white hover:bg-gray-700 disabled:opacity-40"
            >
              {saving ? 'Saving…' : editingId ? 'Save Changes' : 'Create Update'}
            </button>
          </div>
        </div>
      </div>

      {/* List */}
      <div className="rounded-xl border border-gray-200 bg-white shadow-sm">
        <div className="border-b border-gray-100 px-6 py-4">
          <h2 className="text-[15px] font-semibold text-gray-900">All Updates</h2>
        </div>
        {loading ? (
          <div className="px-6 py-8 text-[13px] text-gray-400">Loading…</div>
        ) : updates.length === 0 ? (
          <div className="px-6 py-8 text-[13px] text-gray-400">No updates yet.</div>
        ) : (
          <ul className="divide-y divide-gray-50">
            {updates.map((u) => (
              <li key={u.id} className="px-6 py-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[14px] font-medium text-gray-900">{u.title}</span>
                      <span
                        className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium ${
                          u.placement === 'banner'
                            ? 'bg-purple-50 text-purple-700'
                            : 'bg-gray-100 text-gray-600'
                        }`}
                      >
                        {u.placement}
                      </span>
                      <span
                        className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium ${
                          u.published
                            ? 'bg-green-50 text-green-700'
                            : 'bg-yellow-50 text-yellow-700'
                        }`}
                      >
                        {u.published ? 'published' : 'draft'}
                      </span>
                    </div>
                    {u.body && (
                      <div className={`mt-1 line-clamp-2 ${MD_CLASSES}`}>
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{u.body}</ReactMarkdown>
                      </div>
                    )}
                    <div className="mt-1 text-[11px] text-gray-400">{fmtDate(u.created_at)}</div>
                  </div>
                  <div className="flex shrink-0 items-center gap-3 text-[12px]">
                    <button
                      onClick={() => togglePublished(u)}
                      className="text-gray-500 hover:underline"
                    >
                      {u.published ? 'Unpublish' : 'Publish'}
                    </button>
                    <button onClick={() => startEdit(u)} className="text-blue-600 hover:underline">
                      Edit
                    </button>
                    <button onClick={() => remove(u)} className="text-red-500 hover:underline">
                      Delete
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
