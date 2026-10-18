'use client';

import dynamic from 'next/dynamic';
import { useEffect, useState, type ReactNode } from 'react';
import { useBranding } from '@/components/providers/SiteConfigProvider';
import { getPublicStats, type PublicStats, type PublicStatsResult } from '@/lib/api/publicStats';
import { fill } from '@/lib/utils/interpolate';
import { CONTINENTS, KIND_COLORS, KIND_ORDER } from './format';
import { ModelTrafficSection } from './ModelTraffic';
import { useStatsFormat } from './useStatsFormat';

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

/** A method note: its bold lead, then the explanation. */
function Method({ lead, children }: { lead: string; children: ReactNode }) {
  return (
    <p>
      <span className="font-semibold text-gray-900">{lead}</span> {children}
    </p>
  );
}

function StatsBody({ stats }: { stats: PublicStats }) {
  const f = useStatsFormat();
  const { t } = f;
  const [metric, setMetric] = useState<'tokens' | 'requests'>('tokens');
  const { totals, registrations, models, countries, languages, agents, thresholds } = stats;
  const lastPartial = stats.last_week_partial;
  const withheld = fill(t('stats.withheld', 'fewer than {count}'), {
    count: String(thresholds.min_public_accounts),
  });
  const continents = Object.keys(CONTINENTS)
    .map((code) => ({
      code,
      name: f.continent(code),
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
        <Tile
          label={t('stats.tile.tokens', 'Tokens served')}
          value={f.compact(totals.tokens)}
          note={fill(t('stats.tile.tokens_note', '{requests} requests from {accounts} accounts'), {
            requests: f.compact(totals.requests),
            accounts: f.whole(totals.accounts),
          })}
        />
        {registrations && (
          <>
            <Tile
              label={t('stats.tile.approved', 'Approved users')}
              value={f.whole(registrations.approved)}
              note={t('stats.tile.approved_note', 'Accounts with access to the API')}
            />
            <Tile
              label={t('stats.tile.waiting', 'Waiting list')}
              value={f.whole(registrations.waiting)}
              note={
                registrations.waiting > 0
                  ? t('stats.tile.waiting_note', 'Sign-ups waiting for review')
                  : t('stats.tile.waiting_none', 'No sign-ups are waiting for review')
              }
            />
          </>
        )}
        <Tile
          label={t('stats.tile.countries', 'Countries & territories')}
          value={f.whole(countries.total)}
          note={fill(
            t(
              'stats.tile.countries_note',
              '{count} with {min}+ requests, on {continents} continents',
            ),
            {
              count: String(countries.total_min_requests),
              min: String(thresholds.min_country_requests),
              continents: String(countries.continents),
            },
          )}
        />
        <Tile
          label={t('stats.tile.languages', 'Languages')}
          value={languages ? f.whole(languages.total) : '—'}
          note={
            languages
              ? fill(
                  t(
                    'stats.tile.languages_note',
                    '{count} of {classified} classified accounts also write in a language other than English',
                  ),
                  {
                    count: f.whole(languages.accounts_non_english),
                    classified: f.whole(languages.accounts_classified),
                  },
                )
              : t('stats.tile.languages_none', 'Not measured on this deployment')
          }
        />
        <Tile
          label={t('stats.tile.agents', 'Agent clients')}
          value={f.whole(agents.clients_total)}
          note={fill(
            t(
              'stats.tile.agents_note',
              '{multi} used by more than one account; {products} known agent products',
            ),
            {
              multi: String(agents.clients_multi_account),
              products: String(agents.products_total),
            },
          )}
        />
      </div>

      <section className="space-y-4" aria-labelledby="stats-tokens">
        <SectionHeading id="stats-tokens" title={t('stats.tile.tokens', 'Tokens served')}>
          {fill(
            t(
              'stats.tokens.intro',
              '{tokens} tokens over {days} days: {input} input and {output} output. Agents resend their whole conversation on every turn, so input dominates, and at least {cached} of it was served from the prompt cache.',
            ),
            {
              tokens: f.compact(totals.tokens),
              days: String(stats.window.days),
              input: f.compact(totals.input_tokens),
              output: f.compact(totals.output_tokens),
              cached: f.percent(totals.cached_input_share),
            },
          )}
        </SectionHeading>
        <Card>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-gray-900">
              {metric === 'tokens'
                ? t('stats.tokens_per_day', 'Tokens per day')
                : t('stats.requests_per_day', 'Requests per day')}
            </h3>
            <div
              className="inline-flex rounded-lg border border-gray-200 p-0.5"
              role="group"
              aria-label={t('stats.measure', 'Measure')}
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
                  {m === 'tokens' ? t('stats.tokens', 'Tokens') : t('stats.requests', 'Requests')}
                </button>
              ))}
            </div>
          </div>
          <DailyTokensChart daily={stats.daily} metric={metric} />
          <p className="mt-2 text-xs text-gray-500">
            {t('stats.partial_note', 'The first and last days are partial and drawn lighter.')}
          </p>
        </Card>
      </section>

      {models && <ModelTrafficSection models={models} dates={stats.daily.map((d) => d.date)} />}

      <section className="space-y-4" aria-labelledby="stats-countries">
        <SectionHeading
          id="stats-countries"
          title={t('stats.countries.title', 'Countries and territories')}
        >
          {fill(
            t(
              'stats.countries.intro',
              'Requests came from {count} countries and territories on {continents} continents.',
            ),
            { count: String(countries.total), continents: String(countries.continents) },
          )}
        </SectionHeading>
        <div className="grid items-start gap-4 lg:grid-cols-2">
          <Card>
            <h3 className="text-sm font-semibold text-gray-900">
              {t('stats.active_per_week', 'Active per week')}
            </h3>
            <div className="mb-2 mt-1">
              <Legend
                items={[
                  { label: t('stats.countries.any', 'Any traffic'), color: KIND_COLORS.coding },
                  {
                    label: fill(t('stats.countries.min_week', '{min}+ requests that week'), {
                      min: String(thresholds.min_country_requests),
                    }),
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
                  name: t('stats.countries.any', 'Any traffic'),
                  color: KIND_COLORS.coding,
                  values: countries.weekly.map((w) => w.any),
                },
                {
                  key: 'min',
                  name: fill(t('stats.countries.min', '{min}+ requests'), {
                    min: String(thresholds.min_country_requests),
                  }),
                  color: SECOND_SERIES,
                  values: countries.weekly.map((w) => w.min_requests),
                },
              ]}
            />
          </Card>
          <Card>
            <h3 className="mb-3 text-sm font-semibold text-gray-900">
              {fill(t('stats.countries.top', 'Share of requests, top {count}'), {
                count: String(countries.top.length),
              })}
            </h3>
            <BarList
              rows={countries.top.map((c) => ({
                key: c.code,
                label: f.country(c.code, c.alpha2),
                value: c.share,
                display: f.percent(c.share),
              }))}
            />
          </Card>
        </div>
        <Card>
          <h3 className="mb-3 text-sm font-semibold text-gray-900">
            {t('stats.countries.all', 'Every country and territory seen')}
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
                      title={f.country(row.code, row.alpha2)}
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
            {t('stats.countries.legend', 'Darker means more requests. Hover a code for its name.')}
          </p>
        </Card>
      </section>

      {languages && (
        <section className="space-y-4" aria-labelledby="stats-languages">
          <SectionHeading
            id="stats-languages"
            title={t('stats.languages.title', 'Languages people write in')}
          >
            {fill(
              t(
                'stats.languages.intro',
                'We detected {count} languages in the messages people send. English comes first, but {non_english} of the {classified} accounts we could classify also wrote in another language. The count comes from a sample of {sampled} messages, so it is a lower bound.',
              ),
              {
                count: String(languages.total),
                non_english: f.whole(languages.accounts_non_english),
                classified: f.whole(languages.accounts_classified),
                sampled: f.whole(languages.messages_sampled),
              },
            )}
          </SectionHeading>
          <div className="grid items-start gap-4 lg:grid-cols-2">
            <Card>
              <h3 className="mb-2 text-sm font-semibold text-gray-900">
                {t('stats.languages.weekly', 'Languages detected per week')}
              </h3>
              <WeeklyLinesChart
                weeks={stats.weeks}
                firstPartial={stats.first_week_partial}
                lastPartial={lastPartial}
                series={[
                  {
                    key: 'languages',
                    name: t('stats.tile.languages', 'Languages'),
                    color: KIND_COLORS.coding,
                    values: languages.weekly,
                  },
                ]}
              />
            </Card>
            <Card>
              <h3 className="mb-3 text-sm font-semibold text-gray-900">
                {t('stats.languages.accounts', 'Accounts writing in each language')}
              </h3>
              <BarList
                rows={languages.items.map((item) => {
                  const { name, native } = f.language(item.code);
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
                        : f.whole(item.accounts),
                  };
                })}
              />
            </Card>
          </div>
        </section>
      )}

      <section className="space-y-4" aria-labelledby="stats-agents">
        <SectionHeading id="stats-agents" title={t('stats.agents.title', 'Agents calling the API')}>
          {fill(
            t(
              'stats.agents.intro',
              'Requests named {clients} distinct clients in their User-Agent header, counting each name once across versions and platforms. Matching those names and system prompts against known tools gives {products} agent products.',
            ),
            { clients: f.whole(agents.clients_total), products: String(agents.products_total) },
          )}
        </SectionHeading>
        <div className="grid items-start gap-4 lg:grid-cols-2">
          <Card>
            <h3 className="text-sm font-semibold text-gray-900">
              {t('stats.active_per_week', 'Active per week')}
            </h3>
            <div className="mb-2 mt-1">
              <Legend
                items={[
                  { label: t('stats.agents.all', 'All agents'), color: KIND_COLORS.coding },
                  { label: t('stats.agents.known', 'Known agent products'), color: SECOND_SERIES },
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
                  name: t('stats.agents.all', 'All agents'),
                  color: KIND_COLORS.coding,
                  values: agents.weekly.map((w) => w.clients),
                },
                {
                  key: 'products',
                  name: t('stats.agents.products', 'Agent products'),
                  color: SECOND_SERIES,
                  values: agents.weekly.map((w) => w.products),
                },
              ]}
            />
          </Card>
          <Card>
            <h3 className="text-sm font-semibold text-gray-900">
              {t('stats.agents.share', 'Share of tokens by kind of client')}
            </h3>
            <div className="mb-2 mt-1">
              <Legend
                items={KIND_ORDER.map((kind) => {
                  const share = agents.kinds.find((k) => k.kind === kind)?.token_share ?? 0;
                  return {
                    label: `${f.kind(kind)} ${f.percent(share)}`,
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
          <h3 className="mb-3 text-sm font-semibold text-gray-900">
            {t('stats.agents.products', 'Agent products')}
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-200 text-left text-xs uppercase tracking-wide text-gray-500">
                  <th className="py-2 pr-4 font-medium">{t('stats.agents.col_agent', 'Agent')}</th>
                  <th className="py-2 pr-4 font-medium">{t('stats.agents.col_kind', 'Kind')}</th>
                  <th className="py-2 pr-4 text-right font-medium">
                    {t('stats.tokens', 'Tokens')}
                  </th>
                  <th className="py-2 text-right font-medium">
                    {t('stats.agents.col_accounts', 'Accounts')}
                  </th>
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
                        {p.kind === 'coding'
                          ? t('stats.agents.kind_coding', 'Coding')
                          : t('stats.agents.kind_general', 'General-purpose')}
                      </span>
                    </td>
                    <td className="py-2 pr-4 text-right tabular-nums text-gray-700">
                      {f.compact(p.tokens)}
                    </td>
                    <td className="py-2 text-right tabular-nums text-gray-700">
                      {p.accounts === null
                        ? `<${thresholds.min_public_accounts}`
                        : f.whole(p.accounts)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {chatApps.length > 0 && (
            <p className="mt-3 text-sm text-gray-600">
              {fill(
                chatApps.length === 1
                  ? t('stats.agents.also_seen_one', 'Also seen: {count} chat app ({names}).')
                  : t('stats.agents.also_seen_other', 'Also seen: {count} chat apps ({names}).'),
                { count: String(chatApps.length), names: chatApps.map((p) => p.name).join(', ') },
              )}
            </p>
          )}
        </Card>
      </section>

      <section className="space-y-4" aria-labelledby="stats-method">
        <h2 id="stats-method" className="font-serif text-2xl font-semibold text-gray-900">
          {t('stats.method.title', 'How these numbers are counted')}
        </h2>
        <div className="grid gap-6 text-sm text-gray-600 md:grid-cols-2">
          {registrations && (
            <Method lead={t('stats.method.accounts', 'Accounts.')}>
              {t(
                'stats.method.accounts_body',
                'Approved users are accounts that have been let in; the waiting list is sign-ups still waiting for review. Both are counted when the snapshot is made.',
              )}
            </Method>
          )}
          <Method lead={t('stats.method.tokens', 'Tokens.')}>
            {t(
              'stats.method.tokens_body',
              'Successful requests only, excluding our own health checks. Input counts the whole prompt, including cached prefixes. Weeks start on Monday (UTC).',
            )}
          </Method>
          {models && (
            <Method lead={t('stats.method.models', 'Models.')}>
              {fill(
                t(
                  'stats.method.models_body',
                  'Each request counts under the model it named, so an alias counts as its model and an auto model as itself, not the model chosen to serve it. Models not offered to the public, and models since retired, are counted together as “{other}”.',
                ),
                { other: t('stats.models.other', 'Other models') },
              )}
            </Method>
          )}
          <Method lead={t('stats.method.countries', 'Countries.')}>
            {t(
              'stats.method.countries_body',
              "Each request's IP address is geolocated when it is logged; only the country is kept. Addresses that cannot be placed are left out.",
            )}
          </Method>
          {languages && (
            <Method lead={t('stats.method.languages', 'Languages.')}>
              {t(
                'stats.method.languages_body',
                "Each week we sample up to 8 distinct messages per account, strip code and markup, and identify the language of each sentence with fastText's lid.176 model. A language counts for an account when at least two sampled messages are mainly in it. Romanized writing, such as Hindi in Latin script, cannot be detected.",
              )}
            </Method>
          )}
          <Method lead={t('stats.method.agents', 'Agents.')}>
            {fill(
              t(
                'stats.method.agents_body',
                'A client is the product name at the start of the User-Agent header, ignoring version and platform; generic HTTP libraries, SDKs and browsers are not counted. A client or product needs {min_requests} successful requests to count. Account counts below {min_accounts} are shown as “{withheld}”.',
              ),
              {
                min_requests: String(thresholds.min_client_requests),
                min_accounts: String(thresholds.min_public_accounts),
                withheld,
              },
            )}
          </Method>
        </div>
      </section>
    </div>
  );
}

export function StatsContent(): JSX.Element {
  const branding = useBranding();
  const f = useStatsFormat();
  const { t } = f;
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
          {fill(t('stats.title', '{app_name} usage stats'), { app_name: branding.appName })}
        </h1>
        <p className="max-w-3xl text-gray-600">
          {t(
            'stats.subtitle',
            'Who has access, how much we serve, where requests come from, which languages people write in, and which agents send them. Aggregates only, refreshed daily.',
          )}
        </p>
        {stats && (
          <p className="text-sm text-gray-500">
            {fill(t('stats.window', '{start} – {end} · {days} days · updated {updated}'), {
              start: f.date(stats.window.start),
              end: f.date(stats.window.end),
              days: String(stats.window.days),
              updated: f.date(stats.generated_at),
            })}
          </p>
        )}
      </header>

      {result === null && (
        <p className="text-sm text-gray-500">{t('stats.loading', 'Loading usage stats…')}</p>
      )}
      {result?.status === 'unavailable' && (
        <p className="text-sm text-gray-600">
          {t('stats.unavailable', 'Usage stats are not published for this site yet.')}
        </p>
      )}
      {result?.status === 'error' && (
        <p className="text-sm text-gray-600">
          {t('stats.error', 'Usage stats could not be loaded. Try again in a few minutes.')}
        </p>
      )}
      {stats && <StatsBody stats={stats} />}
    </div>
  );
}
