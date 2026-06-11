import type { AdminRecentRequestItem } from '@/lib/api/admin';
import type { RecentRequestItem } from '@/lib/api/user';

export function formatRouteWiseDecision(
  req: AdminRecentRequestItem | RecentRequestItem,
): string | null {
  const rw = req.routewise;
  if (!rw) return null;

  const providerType = rw.selected_provider_type ?? 'routewise';
  const provider = rw.selected_provider ?? req.provider;
  let hedge = '';
  if (rw.hedging_triggered === true) {
    hedge = `; hedge -> ${rw.hedge_backup_provider ?? rw.hedge_backup_endpoint_id ?? 'backup'}`;
  } else if (rw.hedging_triggered === false) {
    hedge = '; no hedge';
  }

  const backupWon = rw.backup_won ? '; backup won' : '';
  return `${providerType}: ${provider}${hedge}${backupWon}`;
}
