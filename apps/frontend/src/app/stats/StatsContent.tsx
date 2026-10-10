'use client';

import dynamic from 'next/dynamic';
import { useEffect, useState, type ReactNode } from 'react';
import { useBranding } from '@/components/providers/SiteConfigProvider';
import { getPublicStats, type PublicStats, type PublicStatsResult } from '@/lib/api/publicStats';
import {
  CONTINENTS,
  KIND_COLORS,
  KIND_LABELS,
  KIND_ORDER,
  countryName,
  fmtCompact,
  fmtDate,
  fmtPercent,
  fmtWhole,
  languageName,
} from './format';

const chartLoading = () => <div className="h-[200px] animate-pulse rounded-lg bg-gray-50" />;
const DailyTokensChart = dynamic(() => import('./StatsCharts').then((m) => m.DailyTokensChart), {
  ssr: false,
  loading: chartLoading,
});
const WeeklyLinesChart = dynamic(() => import('./StatsCharts').then((m) => m.WeeklyLinesChart), {
  ssr: false,
  loading: chartLoading,
});
const KindShareChart = dynamic(() => import('./StatsCharts').then((m) => m.KindShareChart), {
  ssr: false,
  loading: chartLoading,
});

const SECOND_SERIES = '#eb6834';

function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={`min-w-0 rounded-2xl border border-gray-200 bg-white p-5 shadow-sm sm:p-6 ${className}`}
    >
      {children}
    </div>
  );
}

function Legend({ items }: { items: { label: string; color: string; box?: boolean }[] }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-600">
      {items.map((item) => (
        <span key={item.label} className="inline-flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className={item.box ? 'h-2.5 w-2.5 rounded-sm' : 'h-0.5 w-3.5 rounded-full'}
            style={{ backgroundColor: item.color }}
          />
          {item.label}
        </span>
      ))}
    </div>
  );
}

function BarList({
  rows,
}: {
  rows: { key: string; label: ReactNode; value: number; display: string }[];
}) {
  const max = Math.max(...rows.map((r) => r.value), 1);
  return (
    <ul className="space-y-1.5">
      {rows.map((r) => (
        <li
          key={r.key}
          className="grid grid-cols-[minmax(0,10rem)_minmax(0,1fr)_3.5rem] items-center gap-3 text-sm"
        >
          <span className="truncate text-gray-800">{r.label}</span>
          <span className="h-2.5">
            <span
              className="block h-full rounded-r"
              style={{
                width: `${Math.max(1, (r.value / max) * 100)}%`,
                backgroundColor: KIND_COLORS.coding,
              }}
            />
          </span>
          <span className="text-right text-xs tabular-nums text-gray-600">{r.display}</span>
        </li>
      ))}
    </ul>
  );
}

function Tile({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="min-w-0 rounded-2xl border border-gray-200 bg-white p-5 shadow-sm">
      <p className="text-sm font-medium text-gray-600">{label}</p>
      <p className="mt-1 text-4xl font-semibold tracking-tight text-gray-950">{value}</p>
      <p className="mt-2 text-sm text-gray-600">{note}</p>
    </div>
  );
}

function SectionHeading({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1">
      <h2 id={id} className="font-serif text-2xl font-semibold text-gray-900">
        {title}
      </h2>
      <p className="max-w-3xl text-sm text-gray-600">{children}</p>
    </div>
  );
}

function StatsBody({ stats }: { stats: PublicStats }) {
  const [metric, setMetric] = useState<'tokens' | 'requests'>('tokens');
  const { totals, registrations, countries, languages, agents, thresholds } = stats;
  const lastPartial = stats.last_week_partial;
  const withheld = `fewer than ${thresholds.min_public_accounts}`;
  const continents = Object.entries(CONTINENTS)
    .map(([code, name]) => ({
      code,
      name,
      rows: countries.all.filter((c) => c.continent === code),
    }))
    .filter((c) => c.rows.length > 0);
  const agentProducts = agents.products.filter((p) => p.kind === 'coding' || p.kind === 'general');
  const chatApps = agents.products.filter((p) => p.kind === 'chat');

  return (
    <div className="space-y-12">
      <div
        className={`grid gap-4 sm:grid-cols-2 ${registrations ? 'lg:grid-cols-3' : 'lg:grid-cols-4'}`}
      >
        {registrations && (
          <>
            <Tile
              label="Approved users"
              value={fmtWhole(registrations.approved)}
              note="Accounts with access to the API"
            />
            <Tile
              label="Waiting list"
              value={fmtWhole(registrations.waiting)}
              note={
                registrations.waiting > 0
                  ? 'Sign-ups waiting for review'
                  : 'No sign-ups are waiting for review'
              }
            />
          </>
        )}
        <Tile
          label="Tokens served"
          value={fmtCompact(totals.tokens)}
          note={`${fmtCompact(totals.requests)} requests from ${fmtWhole(totals.accounts)} accounts`}
        />
        <Tile
          label="Countries & territories"
          value={fmtWhole(countries.total)}
          note={`${countries.total_min_requests} with ${thresholds.min_country_requests}+ requests, on ${countries.continents} continents`}
        />
        <Tile
          label="Languages"
          value={languages ? fmtWhole(languages.total) : '—'}
          note={
            languages
              ? `${fmtWhole(languages.accounts_non_english)} of ${fmtWhole(languages.accounts_classified)} classified accounts also write in a language other than English`
              : 'Not measured on this deployment'
          }
        />
        <Tile
          label="Agent clients"
          value={fmtWhole(agents.clients_total)}
          note={`${agents.clients_multi_account} used by more than one account; ${agents.products_total} known agent products`}
        />
      </div>

      <section className="space-y-4" aria-labelledby="stats-tokens">
        <SectionHeading id="stats-tokens" title="Tokens served">
          {fmtCompact(totals.tokens)} tokens over {stats.window.days} days:{' '}
          {fmtCompact(totals.input_tokens)} input and {fmtCompact(totals.output_tokens)} output.
          Agents resend their whole conversation on every turn, so input dominates, and at least{' '}
          {fmtPercent(totals.cached_input_share)} of it was served from the prompt cache.
        </SectionHeading>
        <Card>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-gray-900">
              {metric === 'tokens' ? 'Tokens per day' : 'Requests per day'}
            </h3>
            <div
              className="inline-flex rounded-lg border border-gray-200 p-0.5"
              role="group"
              aria-label="Measure"
            >
              {(['tokens', 'requests'] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  aria-pressed={metric === m}
                  onClick={() => setMetric(m)}
                  className={`rounded-md px-2.5 py-1 text-xs font-medium ${
                    metric === m ? 'bg-gray-900 text-white' : 'text-gray-600 hover:bg-gray-100'
                  }`}
                >
                  {m === 'tokens' ? 'Tokens' : 'Requests'}
                </button>
              ))}
            </div>
          </div>
          <DailyTokensChart daily={stats.daily} metric={metric} />
          <p className="mt-2 text-xs text-gray-500">
            The first and last days are partial and drawn lighter.
          </p>
        </Card>
      </section>

      <section className="space-y-4" aria-labelledby="stats-countries">
        <SectionHeading id="stats-countries" title="Countries and territories">
          Requests came from {countries.total} countries and territories on {countries.continents}{' '}
          continents.
        </SectionHeading>
        <div className="grid items-start gap-4 lg:grid-cols-2">
          <Card>
            <h3 className="text-sm font-semibold text-gray-900">Active per week</h3>
            <div className="mb-2 mt-1">
              <Legend
                items={[
                  { label: 'Any traffic', color: KIND_COLORS.coding },
                  {
                    label: `${thresholds.min_country_requests}+ requests that week`,
                    color: SECOND_SERIES,
                  },
                ]}
              />
            </div>
            <WeeklyLinesChart
              weeks={stats.weeks}
              firstPartial={stats.first_week_partial}
              lastPartial={lastPartial}
              series={[
                {
                  key: 'any',
                  name: 'Any traffic',
                  color: KIND_COLORS.coding,
                  values: countries.weekly.map((w) => w.any),
                },
                {
                  key: 'min',
                  name: `${thresholds.min_country_requests}+ requests`,
                  color: SECOND_SERIES,
                  values: countries.weekly.map((w) => w.min_requests),
                },
              ]}
            />
          </Card>
          <Card>
            <h3 className="mb-3 text-sm font-semibold text-gray-900">
              Share of requests, top {countries.top.length}
            </h3>
            <BarList
              rows={countries.top.map((c) => ({
                key: c.code,
                label: countryName(c.code, c.alpha2),
                value: c.share,
                display: fmtPercent(c.share),
              }))}
            />
          </Card>
        </div>
        <Card>
          <h3 className="mb-3 text-sm font-semibold text-gray-900">
            Every country and territory seen
          </h3>
          <dl className="space-y-3">
            {continents.map((c) => (
              <div key={c.code} className="grid gap-2 text-sm sm:grid-cols-[9rem_minmax(0,1fr)]">
                <dt className="text-gray-600">
                  <span className="font-semibold text-gray-900">{c.rows.length}</span> {c.name}
                </dt>
                <dd className="flex flex-wrap gap-1">
                  {c.rows.map((row) => (
                    <span
                      key={row.code}
                      title={countryName(row.code, row.alpha2)}
                      className="rounded px-1.5 py-0.5 font-mono text-[11px]"
                      style={{
                        backgroundColor: `rgba(42,120,214,${(0.08 + 0.17 * row.level).toFixed(2)})`,
                        color: row.level >= 4 ? '#ffffff' : '#111827',
                      }}
                    >
                      {row.code}
                    </span>
                  ))}
                </dd>
              </div>
            ))}
          </dl>
          <p className="mt-3 text-xs text-gray-500">
            Darker means more requests. Hover a code for its name.
          </p>
        </Card>
      </section>

      {languages && (
        <section className="space-y-4" aria-labelledby="stats-languages">
          <SectionHeading id="stats-languages" title="Languages people write in">
            We detected {languages.total} languages in the messages people send. English comes
            first, but {fmtWhole(languages.accounts_non_english)} of the{' '}
            {fmtWhole(languages.accounts_classified)} accounts we could classify also wrote in
            another language. The count comes from a sample of{' '}
            {fmtWhole(languages.messages_sampled)} messages, so it is a lower bound.
          </SectionHeading>
          <div className="grid items-start gap-4 lg:grid-cols-2">
            <Card>
              <h3 className="mb-2 text-sm font-semibold text-gray-900">
                Languages detected per week
              </h3>
              <WeeklyLinesChart
                weeks={stats.weeks}
                firstPartial={stats.first_week_partial}
                lastPartial={lastPartial}
                series={[
                  {
                    key: 'languages',
                    name: 'Languages',
                    color: KIND_COLORS.coding,
                    values: languages.weekly,
                  },
                ]}
              />
            </Card>
            <Card>
              <h3 className="mb-3 text-sm font-semibold text-gray-900">
                Accounts writing in each language
              </h3>
              <BarList
                rows={languages.items.map((item) => {
                  const { name, native } = languageName(item.code);
                  return {
                    key: item.code,
                    label: (
                      <>
                        {name}
                        {native && <span className="ml-2 text-gray-400">{native}</span>}
                      </>
                    ),
                    value: item.accounts ?? 1,
                    display:
                      item.accounts === null
                        ? `<${thresholds.min_public_accounts}`
                        : fmtWhole(item.accounts),
                  };
                })}
              />
            </Card>
          </div>
        </section>
      )}

      <section className="space-y-4" aria-labelledby="stats-agents">
        <SectionHeading id="stats-agents" title="Agents calling the API">
          Requests named {fmtWhole(agents.clients_total)} distinct clients in their User-Agent
          header, counting each name once across versions and platforms. Matching those names and
          system prompts against known tools gives {agents.products_total} agent products.
        </SectionHeading>
        <div className="grid items-start gap-4 lg:grid-cols-2">
          <Card>
            <h3 className="text-sm font-semibold text-gray-900">Active per week</h3>
            <div className="mb-2 mt-1">
              <Legend
                items={[
                  { label: 'Clients (User-Agent)', color: KIND_COLORS.coding },
                  { label: 'Known agent products', color: SECOND_SERIES },
                ]}
              />
            </div>
            <WeeklyLinesChart
              weeks={stats.weeks}
              firstPartial={stats.first_week_partial}
              lastPartial={lastPartial}
              series={[
                {
                  key: 'clients',
                  name: 'Clients',
                  color: KIND_COLORS.coding,
                  values: agents.weekly.map((w) => w.clients),
                },
                {
                  key: 'products',
                  name: 'Agent products',
                  color: SECOND_SERIES,
                  values: agents.weekly.map((w) => w.products),
                },
              ]}
            />
          </Card>
          <Card>
            <h3 className="text-sm font-semibold text-gray-900">
              Share of tokens by kind of client
            </h3>
            <div className="mb-2 mt-1">
              <Legend
                items={KIND_ORDER.map((kind) => {
                  const share = agents.kinds.find((k) => k.kind === kind)?.token_share ?? 0;
                  return {
                    label: `${KIND_LABELS[kind]} ${fmtPercent(share)}`,
                    color: KIND_COLORS[kind],
                    box: true,
                  };
                })}
              />
            </div>
            <KindShareChart
              weeks={stats.weeks}
              kindWeekly={agents.kind_weekly}
              firstPartial={stats.first_week_partial}
              lastPartial={lastPartial}
            />
          </Card>
        </div>
        <Card>
          <h3 className="mb-3 text-sm font-semibold text-gray-900">Agent products</h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-200 text-left text-xs uppercase tracking-wide text-gray-500">
                  <th className="py-2 pr-4 font-medium">Agent</th>
                  <th className="py-2 pr-4 font-medium">Kind</th>
                  <th className="py-2 pr-4 text-right font-medium">Tokens</th>
                  <th className="py-2 text-right font-medium">Accounts</th>
                </tr>
              </thead>
              <tbody>
                {agentProducts.map((p) => (
                  <tr key={p.name} className="border-b border-gray-100">
                    <td className="py-2 pr-4 text-gray-900">{p.name}</td>
                    <td className="py-2 pr-4 text-gray-600">
                      <span className="inline-flex items-center gap-1.5">
                        <span
                          aria-hidden="true"
                          className="h-2.5 w-2.5 rounded-sm"
                          style={{ backgroundColor: KIND_COLORS[p.kind] }}
                        />
                        {p.kind === 'coding' ? 'Coding' : 'General-purpose'}
                      </span>
                    </td>
                    <td className="py-2 pr-4 text-right tabular-nums text-gray-700">
                      {fmtCompact(p.tokens)}
                    </td>
                    <td className="py-2 text-right tabular-nums text-gray-700">
                      {p.accounts === null
                        ? `<${thresholds.min_public_accounts}`
                        : fmtWhole(p.accounts)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {chatApps.length > 0 && (
            <p className="mt-3 text-sm text-gray-600">
              Also seen: {chatApps.length} chat {chatApps.length === 1 ? 'app' : 'apps'} (
              {chatApps.map((p) => p.name).join(', ')}).
            </p>
          )}
        </Card>
      </section>

      <section className="space-y-4" aria-labelledby="stats-method">
        <h2 id="stats-method" className="font-serif text-2xl font-semibold text-gray-900">
          How these numbers are counted
        </h2>
        <div className="grid gap-6 text-sm text-gray-600 md:grid-cols-2">
          {registrations && (
            <p>
              <span className="font-semibold text-gray-900">Accounts.</span> Approved users are
              accounts that have been let in; the waiting list is sign-ups still waiting for review.
              Both are counted when the snapshot is made.
            </p>
          )}
          <p>
            <span className="font-semibold text-gray-900">Tokens.</span> Successful requests only,
            excluding our own health checks. Input counts the whole prompt, including cached
            prefixes. Weeks start on Monday (UTC).
          </p>
          <p>
            <span className="font-semibold text-gray-900">Countries.</span> Each request&apos;s IP
            address is geolocated when it is logged; only the country is kept. Addresses that cannot
            be placed are left out.
          </p>
          {languages && (
            <p>
              <span className="font-semibold text-gray-900">Languages.</span> Each week we sample up
              to 8 distinct messages per account, strip code and markup, and identify the language
              of each sentence with fastText&apos;s lid.176 model. A language counts for an account
              when at least two sampled messages are mainly in it. Romanized writing, such as Hindi
              in Latin script, cannot be detected.
            </p>
          )}
          <p>
            <span className="font-semibold text-gray-900">Agents.</span> A client is the product
            name at the start of the User-Agent header, ignoring version and platform; generic HTTP
            libraries, SDKs and browsers are not counted. A client or product needs{' '}
            {thresholds.min_client_requests} successful requests to count. Account counts below{' '}
            {thresholds.min_public_accounts} are shown as &ldquo;{withheld}&rdquo;.
          </p>
        </div>
      </section>
    </div>
  );
}

export function StatsContent(): JSX.Element {
  const branding = useBranding();
  const [result, setResult] = useState<PublicStatsResult | null>(null);

  useEffect(() => {
    let active = true;
    getPublicStats().then((r) => {
      if (active) setResult(r);
    });
    return () => {
      active = false;
    };
  }, []);

  const stats = result?.status === 'ok' ? result.stats : null;

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-gray-950">
          {branding.appName} usage stats
        </h1>
        <p className="max-w-3xl text-gray-600">
          Who has access, how much we serve, where requests come from, which languages people write
          in, and which agents send them. Aggregates only, refreshed daily.
        </p>
        {stats && (
          <p className="text-sm text-gray-500">
            {fmtDate(stats.window.start)} – {fmtDate(stats.window.end)} · {stats.window.days} days ·
            updated {fmtDate(stats.generated_at)}
          </p>
        )}
      </header>

      {result === null && <p className="text-sm text-gray-500">Loading usage stats…</p>}
      {result?.status === 'unavailable' && (
        <p className="text-sm text-gray-600">Usage stats are not published for this site yet.</p>
      )}
      {result?.status === 'error' && (
        <p className="text-sm text-gray-600">
          Usage stats could not be loaded. Try again in a few minutes.
        </p>
      )}
      {stats && <StatsBody stats={stats} />}
    </div>
  );
}
