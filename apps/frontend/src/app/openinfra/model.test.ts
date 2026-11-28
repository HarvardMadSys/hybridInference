import { describe, expect, it } from 'vitest';

import {
  deployFromHash,
  filterDash,
  filterHist,
  historyRows,
  hoursFromSearch,
  kpisFromHist,
  parseIndexes,
  preferLive,
  prefillDecodeTps,
  resolveDeploy,
  servedTotals,
  viewFleet,
  NO_HISTORY,
} from './model';
import { dashboard, engine, history } from './testFixtures';

describe('the address', () => {
  it('reads one of the ranges, or any span from 15 minutes to 7 days, else 2 hours', () => {
    expect(hoursFromSearch('')).toBe(2);
    expect(hoursFromSearch('?hours=24')).toBe(24);
    expect(hoursFromSearch('?hours=168')).toBe(168);
    expect(hoursFromSearch('?hours=6')).toBe(6);
    expect(hoursFromSearch('?hours=0.25')).toBe(0.25);
    for (const bad of ['?hours=0.1', '?hours=200', '?hours=abc', '?hours=']) {
      expect(hoursFromSearch(bad), bad).toBe(2);
    }
  });

  it('names a deployment after #, decoded, with the old #fleet meaning none', () => {
    expect(deployFromHash('#chat-model')).toBe('chat-model');
    expect(deployFromHash('#chat%20model')).toBe('chat model');
    expect(deployFromHash('#%E0%A4')).toBe('%E0%A4');
    expect(deployFromHash('#fleet')).toBe('');
    expect(deployFromHash('')).toBe('');
  });
});

describe('the deployments', () => {
  it('parses GPU indexes however the reporter wrote them', () => {
    expect(parseIndexes('0,1')).toEqual([0, 1]);
    expect(parseIndexes('2 3')).toEqual([2, 3]);
    expect(parseIndexes([4, '5'])).toEqual([4, 5]);
    expect(parseIndexes(null)).toEqual([]);
    expect(parseIndexes('')).toEqual([]);
  });

  it('keeps one tab per name, in the snapshot’s order, following the one still reporting', () => {
    const data = dashboard();
    expect(preferLive(data.sglang).map((s) => [s.name, s.id])).toEqual([
      ['chat-model', 'node-a-chat'],
      ['code-model', 'node-a-code'],
      ['embed-model', 'node-b-embed'],
    ]);
    // An offline row listed first gives way to the live one, keeping its place.
    const reordered = [...data.sglang].reverse();
    expect(preferLive(reordered).map((s) => s.id)).toEqual([
      'node-a-chat',
      'node-b-embed',
      'node-a-code',
    ]);
  });

  it('shows the deployment the address names, else the first tab', () => {
    const data = dashboard();
    expect(resolveDeploy(data, 'code-model')).toBe('code-model');
    expect(resolveDeploy(data, 'node-b-embed')).toBe('node-b-embed');
    expect(resolveDeploy(data, 'gone-model')).toBe('chat-model');
    expect(resolveDeploy(data, '')).toBe('chat-model');
    expect(resolveDeploy({ ...data, sglang: [] }, 'chat-model')).toBe('');
  });
});

describe('a deployment’s view', () => {
  it('takes its node, its GPUs, itself and its requests, and recomputes the figures', () => {
    const view = filterDash(dashboard(), 'code-model');
    expect(view.nodes.map((n) => n.id)).toEqual(['node-a']);
    expect(view.gpus.map((g) => g.id)).toEqual(['node-a:2', 'node-a:3']);
    expect(view.sglang.map((s) => s.id)).toEqual(['node-a-code']);
    expect(view.requests.map((r) => r.server_id)).toEqual(['node-a-code']);
    expect(view.kpis.gpus).toBe(2);
    expect(view.kpis.queue_reqs).toBe(9);
    expect(view.kpis.avg_utilization_pct).toBe(52.5);
    expect(view.kpis.memory_total_mb).toBe(160_000);
  });

  it('is the whole snapshot when no deployment is chosen', () => {
    const data = dashboard();
    const view = filterDash(data, '');
    expect(view.gpus).toHaveLength(6);
    expect(view.kpis).toBe(data.kpis);
  });

  it('takes the history rows of the same node, GPUs and engine', () => {
    const data = dashboard();
    const rows = filterHist(historyRows(history()), data, 'embed-model');
    expect(new Set(rows.gpu.map((r) => r.gpu_id))).toEqual(new Set(['node-b:0']));
    expect(new Set(rows.sglang.map((r) => r.server_id))).toEqual(new Set(['node-b-embed']));
    expect(new Set(rows.cpu.map((r) => r.node_id))).toEqual(new Set(['node-b']));
    expect(filterHist(historyRows(history()), data, 'gone-model')).toEqual(NO_HISTORY);
    expect(historyRows(null)).toEqual(NO_HISTORY);
  });

  it('sums what its engines served', () => {
    const totals = servedTotals(filterDash(dashboard(), 'code-model'));
    expect(totals.hour).toEqual({
      reqs: 20,
      cache_tokens: 20_000,
      prefill_tokens: 6_000,
      decode_tokens: 2_000,
    });
    expect(totals.day.reqs).toBe(480);
  });
});

describe('the figures', () => {
  it('rates the view offline, degraded, busy or healthy', () => {
    const data = dashboard();
    const view = filterDash(data, 'chat-model');
    expect(viewFleet(view)).toBe('healthy');
    expect(viewFleet({ ...view, kpis: { ...view.kpis, queue_reqs: 201 } })).toBe('busy');
    expect(viewFleet({ ...view, kpis: { ...view.kpis, avg_utilization_pct: 81 } })).toBe('busy');
    expect(viewFleet({ ...view, kpis: { ...view.kpis, queue_reqs: 1501 } })).toBe('degraded');
    expect(
      viewFleet({ ...view, gpus: view.gpus.map((g, i) => (i ? g : { ...g, status: 'degraded' })) }),
    ).toBe('degraded');
    expect(viewFleet({ ...view, gpus: view.gpus.map((g) => ({ ...g, status: 'offline' })) })).toBe(
      'offline',
    );
  });

  it('averages the range’s history, leaving idle cache-hit points out', () => {
    const data = dashboard();
    const view = filterDash(data, 'chat-model');
    const k = kpisFromHist(view, filterHist(historyRows(history()), data, 'chat-model'));
    expect(k.averaged).toBe(true);
    expect(k.avg_utilization_pct).toBe(50);
    expect(k.running_reqs).toBe(11);
    expect(k.queue_reqs).toBe(2);
    expect(k.cache_hit_rate).toBe(0.6);
    expect(k.memory_used_mb).toBe(0.7 * 160_000);
    expect(k.ram_used_mb).toBe(600_000);
  });

  it('falls back to the snapshot without history', () => {
    const view = filterDash(dashboard(), 'chat-model');
    const k = kpisFromHist(view, NO_HISTORY);
    expect(k.averaged).toBe(false);
    expect(k.queue_reqs).toBe(3);
    expect(k.cache_hit_rate).toBe(0.6);
  });

  it('adds prompt and generated tokens, or has none', () => {
    expect(prefillDecodeTps(engine('x', 'x', 'node-a', '0'))).toBe(6_000);
    expect(prefillDecodeTps({ prefill_tps: '5', gen_throughput: null })).toBe(5);
    expect(prefillDecodeTps({})).toBeNull();
  });
});
