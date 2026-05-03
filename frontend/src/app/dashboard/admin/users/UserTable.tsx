'use client';

import { useMemo, useState, type ReactNode } from 'react';
import type { CostHistoryPoint, Density, FilterState, UserRow as UserRowType } from './types';
import { UserRow } from './UserRow';

interface UserTableProps {
  users: UserRowType[];
  costHistories: Record<string, CostHistoryPoint[]>;
  density: Density;
  filterState: FilterState;
  onSortChange: (sortBy: FilterState['sortBy']) => void;
  /** Quick-action handlers for the row (per-row buttons). */
  onApprove: (userId: string) => void;
  onReject: (user: UserRowType) => void;
  onRegenerateKey: (user: UserRowType) => void;
  /**
   * Render the detail panel for a user. Called when the row is expanded.
   * Returning null hides the panel slot. The parent (UsersTab) is the one
   * that owns detail loading + UserDetailPanel state, so the table just
   * yields the row to the parent.
   */
  renderDetail: (user: UserRowType) => ReactNode;
  /** Optional: notified when a row is expanded so parent can pre-load detail. */
  onExpand?: (userId: string | null) => void;
}

function median(nums: number[]): number {
  if (nums.length === 0) return 0;
  const sorted = [...nums].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
}

export function UserTable(props: UserTableProps) {
  const {
    users,
    costHistories,
    density,
    filterState,
    onSortChange,
    onApprove,
    onReject,
    onRegenerateKey,
    renderDetail,
    onExpand,
  } = props;
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const setExpanded = (next: string | null) => {
    setExpandedId(next);
    onExpand?.(next);
  };

  const pageMedianToday = useMemo(
    () => median(users.map((u) => Number(u.usage_today_usd)).filter((n) => n > 0)),
    [users],
  );

  const showSparkline = density === 'comfortable';
  const colSpan = showSparkline ? 10 : 9;

  const sortIndicator = (col: FilterState['sortBy']) => (filterState.sortBy === col ? '↓' : '');

  return (
    <div className="overflow-x-auto rounded-md border border-gray-200">
      <table className="min-w-full text-sm">
        <thead className="bg-gray-50">
          <tr className="text-left">
            <th className="w-8 px-2 py-2" />
            <th className="px-2 py-2">Email</th>
            <th className="px-2 py-2">Role</th>
            <th
              className="cursor-pointer px-2 py-2"
              onClick={() => onSortChange('cost_today')}
            >
              Today {sortIndicator('cost_today')}
            </th>
            {showSparkline && <th className="w-16 px-2 py-2">7d</th>}
            <th
              className="cursor-pointer px-2 py-2"
              onClick={() => onSortChange('cost_month')}
            >
              Month {sortIndicator('cost_month')}
            </th>
            <th
              className="cursor-pointer px-2 py-2"
              onClick={() => onSortChange('cost_alltime')}
            >
              All-time {sortIndicator('cost_alltime')}
            </th>
            <th className="px-2 py-2">Status</th>
            <th className="w-8 px-2 py-2" />
            <th className="px-2 py-2">Actions</th>
          </tr>
        </thead>
        <tbody>
          {users.length === 0 && (
            <tr>
              <td colSpan={colSpan} className="px-4 py-8 text-center text-gray-500">
                No users match your filters.
              </td>
            </tr>
          )}
          {users.map((u) => {
            const expanded = expandedId === u.id;
            return (
              <RowGroup
                key={u.id}
                user={u}
                expanded={expanded}
                onToggleExpanded={() => setExpanded(expanded ? null : u.id)}
                history={costHistories[u.id]}
                pageMedianToday={pageMedianToday}
                density={density}
                colSpan={colSpan}
                onApprove={() => onApprove(u.id)}
                onReject={() => onReject(u)}
                onRegenerateKey={() => onRegenerateKey(u)}
                renderDetail={renderDetail}
              />
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

interface RowGroupProps {
  user: UserRowType;
  expanded: boolean;
  onToggleExpanded: () => void;
  history: CostHistoryPoint[] | undefined;
  pageMedianToday: number;
  density: Density;
  colSpan: number;
  onApprove: () => void;
  onReject: () => void;
  onRegenerateKey: () => void;
  renderDetail: (user: UserRowType) => ReactNode;
}

function RowGroup({
  user,
  expanded,
  onToggleExpanded,
  history,
  pageMedianToday,
  density,
  colSpan,
  onApprove,
  onReject,
  onRegenerateKey,
  renderDetail,
}: RowGroupProps) {
  return (
    <>
      <UserRow
        user={user}
        history={history}
        pageMedianToday={pageMedianToday}
        density={density}
        expanded={expanded}
        onToggleExpanded={onToggleExpanded}
        onApprove={onApprove}
        onReject={onReject}
        onRegenerateKey={onRegenerateKey}
      />
      {expanded && (
        <tr>
          <td colSpan={colSpan} className="bg-gray-50 p-4">
            {renderDetail(user)}
          </td>
        </tr>
      )}
    </>
  );
}
