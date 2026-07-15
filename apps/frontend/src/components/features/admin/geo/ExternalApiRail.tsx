import type { GeoProvider } from '@/lib/api/admin';

export interface ExternalProviderTraffic {
  provider: GeoProvider;
  requests: number;
}

const numberFormat = new Intl.NumberFormat('en-US');

export function ExternalApiRail({
  traffic,
  onSelect,
}: {
  traffic: ExternalProviderTraffic[];
  onSelect: (providerId: string) => void;
}) {
  const maximum = Math.max(1, ...traffic.map(({ requests }) => requests));

  return (
    <section className="rounded-xl border border-white/10 bg-[#0d111a]/95 p-3 text-white">
      <h3 className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">
        External APIs
      </h3>
      <p className="mt-0.5 text-[11px] text-gray-500">Serving location is not claimed</p>
      <div className="mt-3 space-y-2.5">
        {traffic.length ? (
          traffic.map(({ provider, requests }) => (
            <button
              key={provider.id}
              type="button"
              className="block w-full rounded-md text-left focus:outline-none focus:ring-2 focus:ring-blue-400"
              onClick={() => onSelect(provider.id)}
            >
              <span className="flex items-center justify-between gap-3 text-xs">
                <span className="truncate text-gray-200">{provider.label}</span>
                <strong className="tabular-nums text-white">{numberFormat.format(requests)}</strong>
              </span>
              <span
                aria-hidden="true"
                className="mt-1 block h-1 rounded-full bg-blue-500/80"
                style={{ width: `${Math.max(2, (requests / maximum) * 100)}%` }}
              />
            </button>
          ))
        ) : (
          <p className="text-xs text-gray-500">No external traffic this hour</p>
        )}
      </div>
    </section>
  );
}
