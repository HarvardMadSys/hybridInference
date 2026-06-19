'use client';

import { ProviderRoutesTab } from './ProviderRoutesTab';

export function RoutingTab() {
  return (
    <div className="mt-6 space-y-5">
      <div>
        <h2 className="text-[14px] font-semibold text-gray-900">Routing</h2>
        <p className="mt-1 text-sm text-gray-500">
          Configure model routing policy, provider candidates, RouteWise parameters, and fixed
          weights.
        </p>
      </div>
      <ProviderRoutesTab showRoutewiseSettings />
    </div>
  );
}
