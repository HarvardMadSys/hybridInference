'use client';

import { RoutewiseSettingsPanel } from './RoutewiseSettingsPanel';

export function RoutewiseTab() {
  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-[14px] font-semibold text-gray-900">RouteWise Settings</h2>
        <p className="mt-1 text-sm text-gray-500">Configure RouteWise runtime parameters.</p>
      </div>
      <RoutewiseSettingsPanel />
    </div>
  );
}
