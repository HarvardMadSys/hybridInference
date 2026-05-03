import type { SavedView } from '../types';
import { DEFAULT_FILTER_STATE } from './filterTypes';

export const BUILTIN_VIEWS: SavedView[] = [
  {
    id: 'pending',
    name: 'Pending',
    builtin: true,
    filterState: { ...DEFAULT_FILTER_STATE, status: 'pending_approval', view: 'pending' },
  },
  {
    id: 'top-spenders-today',
    name: 'Top spenders today',
    builtin: true,
    filterState: {
      ...DEFAULT_FILTER_STATE,
      sortBy: 'cost_today',
      view: 'top-spenders-today',
    },
  },
  {
    id: 'anomalies',
    name: 'Anomalies',
    builtin: true,
    filterState: { ...DEFAULT_FILTER_STATE, view: 'anomalies' },
  },
  {
    id: 'near-quota',
    name: 'Near quota',
    builtin: true,
    filterState: { ...DEFAULT_FILTER_STATE, quotaState: 'near', view: 'near-quota' },
  },
  {
    id: 'recently-active',
    name: 'Recently active',
    builtin: true,
    filterState: {
      ...DEFAULT_FILTER_STATE,
      activeWithinHours: 24,
      sortBy: 'last_login',
      view: 'recently-active',
    },
  },
  {
    id: 'new-this-week',
    name: 'New this week',
    builtin: true,
    filterState: {
      ...DEFAULT_FILTER_STATE,
      sortBy: 'created',
      activeWithinHours: 24 * 7,
      view: 'new-this-week',
    },
  },
];

export function getViewById(id: string): SavedView | undefined {
  return BUILTIN_VIEWS.find((v) => v.id === id);
}
