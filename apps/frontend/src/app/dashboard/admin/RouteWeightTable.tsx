'use client';

import type { RouteWeight } from '@/lib/api/admin';

function rowKey(row: Pick<RouteWeight, 'model_id' | 'endpoint_id'>) {
  return `${row.model_id}\u0000${row.endpoint_id}`;
}

function formatWeight(value: number) {
  return String(value);
}

function parseDraftWeight(value: string) {
  if (value.trim() === '') {
    return null;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return null;
  }
  return parsed;
}

export interface RouteWeightTableProps {
  routes: RouteWeight[];
  draftWeights: Record<string, string>;
  savingKeys: Set<string>;
  onDraftChange: (key: string, value: string) => void;
  onSave: (route: RouteWeight) => void;
  onClear: (route: RouteWeight) => void;
}

export function RouteWeightTable({
  routes,
  draftWeights,
  savingKeys,
  onDraftChange,
  onSave,
  onClear,
}: RouteWeightTableProps) {
  const groupedRoutes = new Map<string, RouteWeight[]>();
  for (const route of routes) {
    const group = groupedRoutes.get(route.model_id) ?? [];
    group.push(route);
    groupedRoutes.set(route.model_id, group);
  }

  return Array.from(groupedRoutes.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([modelId, modelRoutes]) => (
      <section key={modelId} className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
        <h2 className="text-[15px] font-semibold text-gray-900">{modelId}</h2>
        <div className="mt-4 overflow-x-auto rounded-md border border-gray-200">
          <table className="min-w-full text-[13px]">
            <thead className="bg-gray-50 text-gray-500">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Endpoint</th>
                <th className="px-3 py-2 text-left font-medium">Provider</th>
                <th className="px-3 py-2 text-left font-medium">YAML</th>
                <th className="px-3 py-2 text-left font-medium">Override</th>
                <th className="px-3 py-2 text-left font-medium">Effective</th>
                <th className="px-3 py-2 text-right font-medium">Runtime Weight</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {modelRoutes.map((route) => {
                const key = rowKey(route);
                const isSaving = savingKeys.has(key);
                const inputValue = draftWeights[key] ?? formatWeight(route.effective_weight);
                const parsedDraft = parseDraftWeight(inputValue);
                const isDirty = parsedDraft !== null && parsedDraft !== route.effective_weight;
                return (
                  <tr key={key} className="bg-white align-top">
                    <td className="px-3 py-2 text-gray-900">
                      <div>{route.endpoint_id}</div>
                      {route.base_url && (
                        <div className="mt-1 max-w-[280px] truncate text-[11px] text-gray-400">
                          {route.base_url}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2 text-gray-600">{route.provider}</td>
                    <td className="px-3 py-2 text-gray-600">{formatWeight(route.yaml_weight)}</td>
                    <td className="px-3 py-2 text-gray-600">
                      {route.override_weight === null ? (
                        'Default'
                      ) : (
                        <span className="font-medium text-amber-700">Override active</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-gray-900">
                      {formatWeight(route.effective_weight)}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <div className="flex justify-end gap-2">
                        <input
                          aria-label={`Runtime weight for ${route.endpoint_id}`}
                          className="w-24 rounded-md border border-gray-300 px-2 py-1 text-right text-[13px] text-gray-900"
                          disabled={isSaving}
                          min={0}
                          step={0.1}
                          type="number"
                          value={inputValue}
                          onChange={(e) => onDraftChange(key, e.target.value)}
                        />
                        <button
                          aria-label={`Save ${route.endpoint_id} weight`}
                          type="button"
                          disabled={isSaving || !isDirty}
                          onClick={() => onSave(route)}
                          className="rounded-md bg-gray-900 px-3 py-1 text-[12px] font-medium text-white disabled:opacity-50"
                        >
                          Save
                        </button>
                        {route.override_weight !== null && (
                          <button
                            aria-label={`Clear ${route.endpoint_id} override`}
                            type="button"
                            disabled={isSaving}
                            onClick={() => onClear(route)}
                            className="rounded-md border border-gray-300 px-2 py-1 text-[12px] font-medium text-gray-600 disabled:opacity-50"
                          >
                            Reset
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    ));
}

export { rowKey };
