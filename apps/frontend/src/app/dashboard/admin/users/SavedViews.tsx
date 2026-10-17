'use client';

import { useState } from 'react';
import { BUILTIN_VIEWS } from './lib/views';
import { useSavedViews } from './hooks/useSavedViews';
import type { FilterState, SavedView } from './types';

interface SavedViewsProps {
  current: FilterState;
  onApply: (state: FilterState) => void;
}

export function SavedViews({ current, onApply }: SavedViewsProps) {
  const { views: customViews, save, remove } = useSavedViews();
  const [showSaveDialog, setShowSaveDialog] = useState(false);

  const all: SavedView[] = [...BUILTIN_VIEWS, ...customViews];
  const activeId = current.view;

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="mr-1 text-[11px] font-medium uppercase tracking-wide text-gray-400">
        Views
      </span>
      {all.map((v) => (
        // Wrap in a div so the two sibling buttons are valid interactive elements
        // (no nested buttons — that's invalid HTML and breaks a11y).
        <div key={v.id} className="flex items-stretch">
          <button
            type="button"
            onClick={() => onApply(v.filterState)}
            className={`rounded-full border px-3 py-1 text-[12px] font-medium transition-colors ${
              v.builtin ? '' : 'rounded-r-none border-r-0'
            } ${
              activeId === v.id
                ? 'border-gray-900 bg-gray-900 text-white'
                : 'border-gray-200 bg-white text-gray-600 hover:border-gray-300 hover:text-gray-900'
            }`}
          >
            {v.name}
          </button>
          {!v.builtin && (
            <button
              type="button"
              aria-label={`Delete view ${v.name}`}
              onClick={() => remove(v.id)}
              className={`rounded-r-full border border-l-0 px-2 text-xs transition-colors ${
                activeId === v.id
                  ? 'border-gray-900 bg-gray-900 text-gray-400 hover:text-white'
                  : 'border-gray-200 bg-white text-gray-400 hover:bg-red-50 hover:text-red-500'
              }`}
            >
              ×
            </button>
          )}
        </div>
      ))}
      <button
        type="button"
        onClick={() => setShowSaveDialog(true)}
        className="rounded-full border border-dashed border-gray-300 px-3 py-1 text-[12px] font-medium text-gray-500 transition hover:border-gray-400 hover:text-gray-900"
      >
        + Save current
      </button>
      {showSaveDialog && (
        <SaveDialog
          onSave={(name) => {
            const baseId = name
              .trim()
              .toLowerCase()
              .replace(/[^a-z0-9]+/g, '-');
            // Ensure the slug is unique: append -2, -3, … if it already exists
            // in either built-in views or existing custom views.
            const existingIds = new Set(all.map((v) => v.id));
            let id = baseId;
            let suffix = 2;
            while (existingIds.has(id)) {
              id = `${baseId}-${suffix}`;
              suffix += 1;
            }
            save({
              id,
              name: name.trim(),
              builtin: false,
              filterState: { ...current, view: id },
            });
            setShowSaveDialog(false);
          }}
          onCancel={() => setShowSaveDialog(false)}
        />
      )}
    </div>
  );
}

function SaveDialog({
  onSave,
  onCancel,
}: {
  onSave: (name: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState('');
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/20 backdrop-blur-[2px]">
      <div className="mx-4 w-full max-w-sm rounded-xl border border-gray-200 bg-white p-5 shadow-2xl">
        <h3 className="mb-3 text-[15px] font-semibold text-gray-900">
          Save current filter as a view
        </h3>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. High-cost free users"
          className="h-9 w-full rounded-md border border-gray-200 px-3 text-[13px] placeholder:text-gray-400 focus:border-gray-400 focus:outline-none"
        />
        <div className="mt-4 flex justify-end gap-2 text-[13px]">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md px-3 py-1.5 text-gray-500 transition hover:text-gray-900"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => name.trim() && onSave(name)}
            disabled={!name.trim()}
            className="rounded-md bg-gray-900 px-3 py-1.5 font-semibold text-white transition hover:bg-gray-800 disabled:opacity-40"
          >
            Save
          </button>
        </div>
      </div>
    </div>
  );
}
