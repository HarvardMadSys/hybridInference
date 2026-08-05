import { describe, expect, it } from "vitest";

import { MODEL_SWEEP_INTERVAL_MS, reconcileModels } from "../src/db";

const NOW = Date.parse("2026-08-05T12:00:00Z");

type Row = { id: number; model_id: string };

/**
 * In-memory D1 double that records every statement it executes, so tests can
 * assert on the *shape* of the SQL (indexed `IN` vs full-scan `NOT IN` vs none
 * at all) and not just on the resulting rows. The read cost this module exists
 * to control is a property of the statement, not of the final state.
 */
class FakeStmt {
  readonly args: unknown[] = [];

  constructor(
    private readonly db: FakeD1,
    readonly sql: string,
  ) {}

  bind(...args: unknown[]): this {
    this.args.push(...args);
    return this;
  }

  async first<T>(): Promise<T | null> {
    if (/SELECT key, value FROM meta WHERE key IN/.test(this.sql)) {
      throw new Error("multi-key read must use all(), not first()");
    }
    throw new Error(`unhandled first: ${this.sql}`);
  }

  async all<T>(): Promise<{ results: T[] }> {
    this.db.executed.push(this);
    if (/SELECT key, value FROM meta WHERE key IN/.test(this.sql)) {
      const rows = (this.args as string[])
        .filter((key) => this.db.meta.has(key))
        .map((key) => ({ key, value: this.db.meta.get(key) }));
      return { results: rows as T[] };
    }
    throw new Error(`unhandled all: ${this.sql}`);
  }

  async run(): Promise<{ meta: { changes: number } }> {
    this.db.executed.push(this);
    if (/INSERT OR REPLACE INTO meta/.test(this.sql)) {
      // Statements here bind the key positionally or inline it as a literal.
      const literal = /VALUES \('([^']+)', \?\)/.exec(this.sql);
      const [key, value] = literal ? [literal[1], this.args[0]] : this.args;
      this.db.meta.set(key as string, String(value));
      return { meta: { changes: 1 } };
    }
    if (/^DELETE FROM probe_results WHERE model_id NOT IN \((\?,?)+\)$/.test(this.sql)) {
      const keep = new Set(this.args as string[]);
      return this.delete((row) => !keep.has(row.model_id));
    }
    if (/^DELETE FROM probe_results WHERE model_id IN \((\?,?)+\)$/.test(this.sql)) {
      const drop = new Set(this.args as string[]);
      return this.delete((row) => drop.has(row.model_id));
    }
    if (/^DELETE FROM probe_results$/.test(this.sql)) {
      return this.delete(() => true);
    }
    throw new Error(`unhandled run: ${this.sql}`);
  }

  private delete(match: (row: Row) => boolean): { meta: { changes: number } } {
    const before = this.db.probe.length;
    this.db.probe = this.db.probe.filter((row) => !match(row));
    return { meta: { changes: before - this.db.probe.length } };
  }
}

class FakeD1 {
  readonly meta = new Map<string, string>();
  readonly executed: FakeStmt[] = [];
  probe: Row[] = [];
  private seq = 0;

  prepare(sql: string): FakeStmt {
    return new FakeStmt(this, sql);
  }

  async batch(stmts: FakeStmt[]): Promise<Array<{ meta: { changes: number } }>> {
    const out = [];
    for (const stmt of stmts) out.push(await stmt.run());
    return out;
  }

  /** Seeds probe history for `modelId`, as a cycle's `recordResults` would. */
  record(...modelIds: string[]): this {
    for (const modelId of modelIds) this.probe.push({ id: ++this.seq, model_id: modelId });
    return this;
  }

  /** Seeds the `meta.model_ids` row a prior successful cycle would have left. */
  storeModelIds(ids: string[], sweptAtMs: number | null = NOW): this {
    this.meta.set("model_ids", JSON.stringify([...ids].sort((a, b) => a.localeCompare(b))));
    if (sweptAtMs !== null) this.meta.set("model_ids_swept_at", String(sweptAtMs));
    return this;
  }

  get modelIds(): string[] {
    return JSON.parse(this.meta.get("model_ids") ?? "null");
  }

  /** Every probe_results statement issued — the rows_read-bearing work. */
  get probeStatements(): string[] {
    return this.executed.map((s) => s.sql).filter((sql) => /probe_results/.test(sql));
  }

  as(): D1Database {
    return this as unknown as D1Database;
  }
}

describe("reconcileModels", () => {
  it("issues no probe_results statement when the active set is unchanged", async () => {
    // The steady state: 72 cycles/day all reconciling an identical catalog. A
    // full-scan DELETE here bills rows_read ~ the whole retained history, every
    // cycle, to delete nothing.
    const db = new FakeD1().record("a", "b", "c").storeModelIds(["a", "b", "c"]);

    await reconcileModels(db.as(), ["c", "a", "b"], NOW);

    expect(db.probeStatements).toEqual([]);
    expect(db.probe).toHaveLength(3);
  });

  it("deletes removed models by indexed IN rather than scanning with NOT IN", async () => {
    const db = new FakeD1().record("a", "b", "gone").storeModelIds(["a", "b", "gone"]);

    await reconcileModels(db.as(), ["a", "b"], NOW);

    // `model_id IN (...)` seeks idx_probe_results_model_id, so rows_read is
    // proportional to the rows actually deleted; `NOT IN` cannot use the index.
    expect(db.probeStatements).toEqual(["DELETE FROM probe_results WHERE model_id IN (?)"]);
    expect(db.executed.at(-2)?.args).toEqual(["gone"]);
    expect(db.probe.map((r) => r.model_id)).toEqual(["a", "b"]);
    expect(db.modelIds).toEqual(["a", "b"]);
  });

  it("records a newly added model without touching probe_results", async () => {
    const db = new FakeD1().record("a").storeModelIds(["a"]);

    await reconcileModels(db.as(), ["a", "new"], NOW);

    expect(db.probeStatements).toEqual([]);
    expect(db.modelIds).toEqual(["a", "new"]);
  });

  it("sweeps with NOT IN when meta.model_ids is absent, collecting orphans", async () => {
    // First deploy against an existing database: there is no previous set to
    // diff, so the one-off full sweep is the only way to evict stale history.
    const db = new FakeD1().record("a", "orphan");

    await reconcileModels(db.as(), ["a"], NOW);

    expect(db.probeStatements).toEqual(["DELETE FROM probe_results WHERE model_id NOT IN (?)"]);
    expect(db.probe.map((r) => r.model_id)).toEqual(["a"]);
    expect(db.modelIds).toEqual(["a"]);
  });

  it("sweeps with NOT IN when meta.model_ids is corrupt", async () => {
    const db = new FakeD1().record("a", "orphan");
    db.meta.set("model_ids", "{not json");
    db.meta.set("model_ids_swept_at", String(NOW));

    await reconcileModels(db.as(), ["a"], NOW);

    expect(db.probeStatements).toEqual(["DELETE FROM probe_results WHERE model_id NOT IN (?)"]);
    expect(db.probe.map((r) => r.model_id)).toEqual(["a"]);
  });

  it("sweeps once the interval elapses, collecting rows the diff cannot see", async () => {
    // A cycle that dies between recordResults and reconcileModels leaves rows
    // whose model_id was never in the stored set, so no future diff would ever
    // name them. The periodic sweep is the backstop that bounds that leak.
    const db = new FakeD1().record("a", "orphan").storeModelIds(["a"], NOW - MODEL_SWEEP_INTERVAL_MS);

    await reconcileModels(db.as(), ["a"], NOW);

    expect(db.probeStatements).toEqual(["DELETE FROM probe_results WHERE model_id NOT IN (?)"]);
    expect(db.probe.map((r) => r.model_id)).toEqual(["a"]);
    expect(db.meta.get("model_ids_swept_at")).toBe(String(NOW));
  });

  it("does not sweep again before the interval elapses", async () => {
    const db = new FakeD1()
      .record("a", "orphan")
      .storeModelIds(["a"], NOW - MODEL_SWEEP_INTERVAL_MS + 1);

    await reconcileModels(db.as(), ["a"], NOW);

    expect(db.probeStatements).toEqual([]);
    expect(db.meta.get("model_ids_swept_at")).toBe(String(NOW - MODEL_SWEEP_INTERVAL_MS + 1));
  });

  it("clears all probe rows when the catalog is legitimately empty", async () => {
    const db = new FakeD1().record("a", "b").storeModelIds(["a", "b"]);

    await reconcileModels(db.as(), [], NOW);

    expect(db.probeStatements).toEqual(["DELETE FROM probe_results"]);
    expect(db.probe).toEqual([]);
    expect(db.modelIds).toEqual([]);
  });

  it("rewrites a stored set whose representation drifted, without deleting anything", async () => {
    // A legacy row written unsorted still names the same models, so nothing has
    // departed — but the row should be normalized so the dashboard's read order
    // is stable.
    const db = new FakeD1().record("a", "b");
    db.meta.set("model_ids", JSON.stringify(["b", "a"]));
    db.meta.set("model_ids_swept_at", String(NOW));

    await reconcileModels(db.as(), ["a", "b"], NOW);

    expect(db.probeStatements).toEqual([]);
    expect(db.modelIds).toEqual(["a", "b"]);
  });

  it("reads both meta keys in a single keyed statement", async () => {
    const db = new FakeD1().storeModelIds(["a"]);

    await reconcileModels(db.as(), ["a"], NOW);

    const reads = db.executed.filter((s) => /^SELECT/.test(s.sql));
    expect(reads).toHaveLength(1);
    expect(reads[0].args).toEqual(["model_ids", "model_ids_swept_at"]);
  });
});
