// @vitest-environment jsdom
import { renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { buildCharts, ROLE } from './charts';
import { useInfraFormat } from './format';
import { filterHist, historyRows, NO_HISTORY } from './model';
import { dashboard, history } from './testFixtures';

function format() {
  return renderHook(() => useInfraFormat()).result.current;
}

describe('buildCharts', () => {
  it('draws every chart of the three sections, in the page’s order', () => {
    const charts = buildCharts(NO_HISTORY, format());
    expect(charts.host.map((c) => c.id)).toEqual(['cpu', 'ram']);
    expect(charts.gpu.map((c) => c.id)).toEqual([
      'gpu-sm',
      'gpu-vram',
      'gpu-membw',
      'gpu-power',
      'gpu-temp',
      'gpu-clock',
      'dcgm-tensor',
      'dcgm-dram',
      'dcgm-nvlink',
      'dcgm-pcie',
    ]);
    expect(charts.engine).toHaveLength(18);
    for (const chart of [...charts.host, ...charts.gpu, ...charts.engine]) {
      expect(chart.title, chart.id).not.toBe('');
      expect(chart.desc, chart.id).not.toBe('');
      expect(chart.series, chart.id).toEqual([]);
    }
  });

  it('names a series by its node only when the view spans nodes', () => {
    const f = format();
    const data = dashboard();
    const one = buildCharts(filterHist(historyRows(history()), data, 'chat-model'), f);
    expect(one.gpu[0].series.map((s) => s.label)).toEqual(['GPU 0', 'GPU 1']);
    expect(one.host[0].series.map((s) => s.label)).toEqual(['util %', 'load1']);

    const all = buildCharts(historyRows(history()), f);
    expect(all.gpu[0].series.map((s) => s.label)).toContain('node-b GPU 1');
    expect(all.host[0].series.map((s) => s.label)).toContain('node-a util %');
    expect(all.engine[0].series.map((s) => s.label)).toContain('node-b embed-model queued');
  });

  it('turns ratios into percents and rates into per-minute counts', () => {
    const f = format();
    const charts = buildCharts(filterHist(historyRows(history()), dashboard(), 'chat-model'), f);
    const byId = Object.fromEntries(charts.engine.map((c) => [c.id, c]));
    // token_usage 0.5 is a 50% KV fill.
    expect(byId['kv-cache'].series[0].points[0].v).toBe(50);
    // (2,000 + 3,000 + 1,000) tokens a second, a minute.
    expect(byId['tokens-per-min'].series[0].points[0].v).toBe(360_000);
    // The requests chart is never shorter than four.
    expect(byId.requests.opts.max).toBe(12);
    // TTFT's parts keep their colors whatever the deployment.
    expect(byId.ttft.series.map((s) => s.color)).toEqual([ROLE.second, ROLE.first, ROLE.third]);
    expect(byId.ttft.opts.format?.(0.5)).toBe('0.50s');
  });
});
