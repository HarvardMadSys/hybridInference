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
    <div className="flex flex-wrap items-center gap-2">
      {all.map((v) => (
        // Wrap in a div so the two sibling buttons are valid interactive elements
        // (no nested buttons — that's invalid HTML and breaks a11y).
        <div key={v.id} className="flex items-stretch">
          <button
            type="button"
            onClick={() => onApply(v.filterState)}
            className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
              v.builtin ? '' : 'rounded-r-none border-r-0'
            } ${
              activeId === v.id
                ? 'border-blue-600 bg-blue-600 text-white'
                : 'border-gray-300 bg-white text-gray-700 hover:bg-gray-50'
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
                  ? 'border-blue-600 bg-blue-600 text-blue-200 hover:text-white'
                  : 'border-gray-300 bg-white text-gray-400 hover:bg-red-50 hover:text-red-500'
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
        className="rounded-full border border-dashed border-gray-400 px-3 py-1 text-xs font-medium text-gray-600 hover:bg-gray-50"
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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="w-80 rounded-lg bg-white p-4 shadow-lg">
        <h3 className="mb-2 text-sm font-semibold">Save current filter as a view</h3>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. High-cost free users"
          className="w-full rounded border px-2 py-1 text-sm"
        />
        <div className="mt-3 flex justify-end gap-2 text-sm">
          <button type="button" onClick={onCancel} className="rounded bg-gray-100 px-3 py-1">
            Cancel
          </button>
          <button
            type="button"
            onClick={() => name.trim() && onSave(name)}
            disabled={!name.trim()}
            className="rounded bg-blue-600 px-3 py-1 text-white disabled:bg-gray-300"
          >
            Save
          </button>
        </div>
      </div>
    </div>
  );
}
