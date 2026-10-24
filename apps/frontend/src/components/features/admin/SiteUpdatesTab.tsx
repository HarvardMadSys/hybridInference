'use client';

import { useCallback, useEffect, useState } from 'react';
import { useAccountToast } from '@/lib/hooks/useAccountToast';
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

/** Create a new update, or edit the one with this id. */
type Editor = { mode: 'create' } | { mode: 'edit'; id: string };

function fmtDate(s: string): string {
  return new Date(s).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

function savedInput(u: SiteUpdateItem): SiteUpdateInput {
  return {
    title: u.title,
    body: u.body,
    placement: u.placement,
    published: u.published,
    link_url: u.link_url,
    link_label: u.link_label,
  };
}

function where(placement: SiteUpdatePlacement): string {
  return placement === 'banner' ? 'as the homepage banner' : 'in the homepage Updates section';
}

const MD_CLASSES =
  'text-[13px] text-gray-700 [&_a]:text-blue-600 [&_a]:underline [&_h1]:text-lg [&_h1]:font-semibold [&_h2]:text-base [&_h2]:font-semibold [&_p]:my-2 [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5';

const INPUT_CLASSES =
  'w-full rounded-md border border-gray-200 px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-gray-900 disabled:bg-gray-50';

/**
 * Homepage updates. The list is read-only; New update opens a form that is
 * reviewed before it is created, Edit opens one update's fields as drafts that
 * are saved after the administrator confirms `old → new`, and Publish /
 * Unpublish / Delete are each confirmed on their own.
 */
export function SiteUpdatesTab() {
  const toast = useAccountToast();
  const confirmation = useConfirmAction();
  const [updates, setUpdates] = useState<SiteUpdateItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [editor, setEditor] = useState<Editor | null>(null);
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

  const busy = confirmation.busy;
  const editing = editor?.mode === 'edit' ? updates.find((u) => u.id === editor.id) : undefined;
  const changes = editing ? diffFields<UpdateFields>(savedInput(editing), form, FIELDS) : [];
  // An editor whose update is gone (deleted elsewhere) counts as closed.
  const editorOpen = editor?.mode === 'create' || editing !== undefined;

  function closeEditor() {
    if (busy) return;
    setEditor(null);
    setForm(EMPTY_FORM);
  }

  function startCreate() {
    setEditor({ mode: 'create' });
    setForm(EMPTY_FORM);
  }

  function startEdit(u: SiteUpdateItem) {
    setEditor({ mode: 'edit', id: u.id });
    setForm(savedInput(u));
    if (typeof window !== 'undefined') window.scrollTo({ top: 0, behavior: 'smooth' });
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
        setEditor(null);
        setForm(EMPTY_FORM);
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
        setEditor(null);
        setForm(EMPTY_FORM);
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

  const fieldId = (name: string) => `site-update-${name}`;

  return (
    <div className="mt-8 space-y-6">
      {/* Composer: only after New update or Edit */}
      {editor && editorOpen ? (
        <section
          aria-label={editor.mode === 'create' ? 'New update' : 'Edit update'}
          className="rounded-xl border border-gray-400 bg-white p-6 shadow-sm"
        >
          <h2 className="mb-4 text-[15px] font-semibold text-gray-900">
            {editor.mode === 'create' ? 'New Update' : 'Edit Update'}
          </h2>

          <div className="mb-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label
                htmlFor={fieldId('title')}
                className="mb-1 block text-[12px] font-medium text-gray-600"
              >
                Title
              </label>
              <input
                id={fieldId('title')}
                className={INPUT_CLASSES}
                placeholder="e.g. New model: Llama 4 available"
                value={form.title}
                disabled={busy}
                onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))}
              />
            </div>
            <div>
              <label
                htmlFor={fieldId('placement')}
                className="mb-1 block text-[12px] font-medium text-gray-600"
              >
                Placement
              </label>
              <select
                id={fieldId('placement')}
                value={form.placement}
                disabled={busy}
                onChange={(e) =>
                  setForm((f) => ({ ...f, placement: e.target.value as SiteUpdatePlacement }))
                }
                className={INPUT_CLASSES}
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
            <label
              htmlFor={fieldId('body')}
              className="mb-1 block text-[12px] font-medium text-gray-600"
            >
              Body (Markdown)
            </label>
            <textarea
              id={fieldId('body')}
              rows={5}
              className={`${INPUT_CLASSES} font-mono`}
              placeholder={
                'Describe the update. **Markdown** and [links](https://...) are supported.'
              }
              value={form.body}
              disabled={busy}
              onChange={(e) => setForm((f) => ({ ...f, body: e.target.value }))}
            />
            {form.body && (
              <div className="mt-2">
                <span className="mb-1 block text-[12px] font-medium text-gray-600">Preview</span>
                <div className={`rounded-md border border-gray-200 px-3 py-2 ${MD_CLASSES}`}>
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{form.body}</ReactMarkdown>
                </div>
              </div>
            )}
          </div>

          <div className="mb-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label
                htmlFor={fieldId('link-url')}
                className="mb-1 block text-[12px] font-medium text-gray-600"
              >
                Link URL (optional)
              </label>
              <input
                id={fieldId('link-url')}
                className={INPUT_CLASSES}
                placeholder="https://docs.example.com/..."
                value={form.link_url ?? ''}
                disabled={busy}
                onChange={(e) => setForm((f) => ({ ...f, link_url: e.target.value || null }))}
              />
            </div>
            <div>
              <label
                htmlFor={fieldId('link-label')}
                className="mb-1 block text-[12px] font-medium text-gray-600"
              >
                Link Label (optional)
              </label>
              <input
                id={fieldId('link-label')}
                className={INPUT_CLASSES}
                placeholder="e.g. Read more"
                value={form.link_label ?? ''}
                disabled={busy}
                onChange={(e) => setForm((f) => ({ ...f, link_label: e.target.value || null }))}
              />
            </div>
          </div>

          <label className="flex items-center gap-2 text-[13px] text-gray-700">
            <input
              type="checkbox"
              checked={form.published}
              disabled={busy}
              onChange={(e) => setForm((f) => ({ ...f, published: e.target.checked }))}
            />
            Published (visible on homepage)
          </label>

          {editor.mode === 'edit' && editing ? (
            <EditBar
              target={editing.title}
              changeCount={changes.length}
              onCancel={closeEditor}
              onSave={reviewEdit}
              busy={busy}
            />
          ) : (
            <div className="mt-3 flex flex-wrap items-center justify-end gap-3 border-t border-gray-100 pt-3">
              <button
                type="button"
                onClick={closeEditor}
                disabled={busy}
                className="rounded-md border border-gray-300 px-4 py-2 text-[13px] font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={reviewCreate}
                disabled={busy}
                className="rounded-md bg-gray-900 px-4 py-2 text-[13px] font-medium text-white hover:bg-gray-700 disabled:opacity-40"
              >
                Create Update
              </button>
            </div>
          )}
        </section>
      ) : null}

      {/* List */}
      <div className="rounded-xl border border-gray-200 bg-white shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-100 px-6 py-4">
          <h2 className="text-[15px] font-semibold text-gray-900">All Updates</h2>
          {editorOpen ? null : (
            <button
              type="button"
              onClick={startCreate}
              className="rounded-md bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-white hover:bg-gray-700"
            >
              New update
            </button>
          )}
        </div>
        {loading ? (
          <div className="px-6 py-8 text-[13px] text-gray-400">Loading…</div>
        ) : updates.length === 0 ? (
          <div className="px-6 py-8 text-[13px] text-gray-400">No updates yet.</div>
        ) : (
          <ul className="divide-y divide-gray-50">
            {updates.map((u) => {
              // Drafts of an open editor would be lost or go stale, so the row
              // being edited keeps its actions until the editor closes, and no
              // other update can be opened over it.
              const isEdited = editing?.id === u.id;
              return (
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
                        type="button"
                        onClick={() => confirmTogglePublished(u)}
                        disabled={isEdited}
                        aria-label={`${u.published ? 'Unpublish' : 'Publish'} ${u.title}`}
                        className="text-gray-500 hover:underline disabled:opacity-40"
                      >
                        {u.published ? 'Unpublish' : 'Publish'}
                      </button>
                      <button
                        type="button"
                        onClick={() => startEdit(u)}
                        disabled={editorOpen}
                        aria-label={`Edit ${u.title}`}
                        className="text-blue-600 hover:underline disabled:opacity-40"
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        onClick={() => confirmRemove(u)}
                        disabled={isEdited}
                        aria-label={`Delete ${u.title}`}
                        className="text-red-500 hover:underline disabled:opacity-40"
                      >
                        Delete
                      </button>
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
      {confirmation.dialog}
    </div>
  );
}
