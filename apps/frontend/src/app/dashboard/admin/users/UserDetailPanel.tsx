'use client';

import type { AdminModelVisibilityItem, AdminUser, UserDetail } from '@/lib/api/admin';

function relTime(s: string | null): string {
  if (!s) return 'Never';
  const ms = Date.now() - new Date(s).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  return new Date(s).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

export interface UserDetailPanelProps {
  user: AdminUser;
  detail: UserDetail;
  editRole: string;
  editQuota: string;
  editDisabledModels: string[];
  availableModels: AdminModelVisibilityItem[];
  saving: boolean;
  busy: string | null;
  onChangeRole: (role: string) => void;
  onChangeQuota: (quota: string) => void;
  onChangeDisabledModels: (modelIds: string[]) => void;
  onSave: () => void;
  onSuspend: (userId: string) => void;
  onReactivate: (userId: string) => void;
  onResume: (user: AdminUser) => void;
  onRequestDelete: (user: AdminUser) => void;
  onRequestHardDelete: (user: AdminUser) => void;
}

export function UserDetailPanel(props: UserDetailPanelProps) {
  const {
    user: u,
    detail,
    editRole,
    editQuota,
    editDisabledModels,
    availableModels,
    saving,
    busy,
    onChangeRole,
    onChangeQuota,
    onChangeDisabledModels,
    onSave,
    onSuspend,
    onReactivate,
    onResume,
    onRequestDelete,
    onRequestHardDelete,
  } = props;

  const toggleDisabledModel = (modelId: string) => {
    if (editDisabledModels.includes(modelId)) {
      onChangeDisabledModels(editDisabledModels.filter((value) => value !== modelId));
      return;
    }
    onChangeDisabledModels([...editDisabledModels, modelId].sort());
  };

  return (
    <div className="space-y-4 rounded-xl border border-gray-200 bg-gray-50 p-5">
      {/* Stats */}
      <div className="grid grid-cols-4 gap-3 text-[13px]">
        <div>
          <div className="text-[11px] font-medium text-gray-500">Today</div>
          <div className="mt-0.5 text-[16px] font-bold tabular-nums text-gray-900">
            ${Number(detail.usage_today_usd).toFixed(2)}
          </div>
          <div className="text-[11px] text-gray-400 tabular-nums">
            {detail.usage_today_requests} req
          </div>
        </div>
        <div>
          <div className="text-[11px] font-medium text-gray-500">This month</div>
          <div className="mt-0.5 text-[16px] font-bold tabular-nums text-gray-900">
            ${Number(detail.usage_month_usd).toFixed(2)}
          </div>
          <div className="text-[11px] text-gray-400 tabular-nums">
            {detail.usage_month_requests} req
          </div>
        </div>
        <div>
          <div className="text-[11px] font-medium text-gray-500">Last active</div>
          <div className="mt-0.5 text-[16px] font-bold text-gray-900">
            {relTime(detail.last_request_at)}
          </div>
        </div>
        <div>
          <div className="text-[11px] font-medium text-gray-500">Quota</div>
          <div className="mt-0.5 text-[16px] font-bold text-gray-900">
            {detail.quota_daily_usd ? `$${detail.quota_daily_usd}/d` : '-'}
          </div>
        </div>
      </div>

      {/* Models */}
      {detail.models_used.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {detail.models_used.map((m) => (
            <span
              key={m}
              className="rounded bg-white px-2 py-0.5 text-[11px] font-medium text-gray-600 border border-gray-200 shadow-sm"
            >
              {m.split('/').pop()}
            </span>
          ))}
        </div>
      )}

      {u.status === 'active' && availableModels.length > 0 && (
        <div
          className="space-y-2 border-t border-gray-200 pt-4"
          data-testid="disabled-models-panel"
        >
          <div>
            <div className="text-[11px] font-medium text-gray-500">Model access</div>
            <p className="mt-1 text-[12px] text-gray-500">
              Disabled models stay hidden for this user even if their role would normally allow
              them.
            </p>
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            {availableModels.map((model) => {
              const isDisabled = editDisabledModels.includes(model.model_id);
              return (
                <label
                  key={model.model_id}
                  className={`flex items-center gap-2 rounded-md border px-3 py-2 text-[12px] cursor-pointer ${
                    isDisabled
                      ? 'border-red-200 bg-red-50 text-gray-500'
                      : 'border-gray-200 bg-white text-gray-700'
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={!isDisabled}
                    onChange={() => toggleDisabledModel(model.model_id)}
                    aria-label={isDisabled ? `Enable ${model.model_id}` : `Disable ${model.model_id}`}
                  />
                  <span className={`font-medium ${isDisabled ? 'line-through text-gray-400' : 'text-gray-900'}`}>
                    {model.model_id}
                  </span>
                  {isDisabled && (
                    <span className="ml-auto text-[10px] font-semibold text-red-500">disabled</span>
                  )}
                </label>
              );
            })}
          </div>
        </div>
      )}

      {/* Edit (active users) */}
      {u.status === 'active' && (
        <div className="flex items-end gap-3 border-t border-gray-200 pt-4">
          <div>
            <div className="text-[11px] font-medium text-gray-500 mb-1">Role</div>
            <select
              value={editRole}
              onChange={(e) => onChangeRole(e.target.value)}
              className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-[13px]"
            >
              <option value="free">free</option>
              <option value="pro">pro</option>
              <option value="internal">internal</option>
              <option value="admin">admin</option>
            </select>
          </div>
          {detail.has_key && (
            <>
              <div>
                <div className="text-[11px] font-medium text-gray-500 mb-1">Daily quota</div>
                <input
                  type="number"
                  value={editQuota}
                  onChange={(e) => onChangeQuota(e.target.value)}
                  className="w-24 rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-[13px]"
                />
              </div>
            </>
          )}
          <button
            onClick={onSave}
            disabled={saving}
            className="rounded-md bg-gray-900 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-gray-800 transition disabled:opacity-50"
          >
            {saving ? '...' : 'Save'}
          </button>
          <div className="flex-1" />
          <button
            onClick={() => onRequestDelete(u)}
            className="text-[12px] text-red-400 hover:text-red-600 transition"
          >
            Delete
          </button>
          <button
            onClick={() => onSuspend(u.id)}
            className="text-[12px] text-red-400 hover:text-red-600 transition"
          >
            Suspend
          </button>
        </div>
      )}

      {/* Suspended users */}
      {u.status === 'suspended' && (
        <div className="flex items-center justify-between border-t border-gray-200 pt-4">
          <span className="text-[13px] text-gray-500">This user is suspended.</span>
          <div className="flex items-center gap-3">
            <button
              onClick={() => onRequestDelete(u)}
              className="text-[12px] text-red-400 hover:text-red-600 transition"
            >
              Delete
            </button>
            <button
              onClick={() => onReactivate(u.id)}
              className="rounded-md bg-gray-900 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-gray-800 transition"
            >
              Reactivate
            </button>
          </div>
        </div>
      )}

      {/* Deleted users */}
      {u.status === 'deleted' && (
        <div className="flex items-center justify-between border-t border-gray-200 pt-4">
          <span className="text-[13px] text-gray-400">This user has been deleted.</span>
          <div className="flex items-center gap-3">
            <button
              onClick={() => onRequestHardDelete(u)}
              disabled={busy === u.id}
              className="text-[12px] text-red-500 hover:text-red-700 transition disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Permanently Delete
            </button>
            <button
              onClick={() => onResume(u)}
              disabled={busy === u.id}
              className="rounded-md bg-gray-900 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-gray-800 transition disabled:opacity-50"
            >
              Resume
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
