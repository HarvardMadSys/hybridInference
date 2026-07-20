import { describe, expect, it, vi } from "vitest";

import { D1RelayStore } from "../src/store";

class FakeStatement {
  readonly args: unknown[] = [];

  constructor(readonly sql: string) {}

  bind(...args: unknown[]): this {
    this.args.push(...args);
    return this;
  }
}

function fakeDatabase(claimChanges: number): {
  db: D1Database;
  batch: ReturnType<typeof vi.fn>;
} {
  const batch = vi.fn(async (statements: FakeStatement[]) => {
    const incidentChanges =
      claimChanges === 1 &&
      statements[1].args[0] === statements[1].args[3] &&
      statements[1].args[2] === statements[0].args[3] &&
      statements[1].args[4] === statements[0].args[1]
        ? 1
        : 0;
    return [
      { meta: { changes: claimChanges } },
      { meta: { changes: incidentChanges } },
    ];
  });
  return {
    db: {
      prepare: (sql: string) => new FakeStatement(sql),
      batch,
    } as unknown as D1Database,
    batch,
  };
}

describe("D1RelayStore.setAnalysisRef", () => {
  it("atomically claims the job and conditionally updates its incident", async () => {
    const { db, batch } = fakeDatabase(1);
    const store = new D1RelayStore(db);

    await expect(
      store.setAnalysisRef(
        "22222222-2222-4222-8222-222222222222",
        "a".repeat(40),
        2,
        "2026-07-20T00:00:00.000Z",
      ),
    ).resolves.toBe(true);

    expect(batch).toHaveBeenCalledOnce();
    const statements = batch.mock.calls[0][0] as FakeStatement[];
    expect(statements).toHaveLength(2);
    expect(statements[1].sql).toContain("WHERE changes() = 1");
    expect(statements[1].sql).toContain("status = 'dispatching'");
    expect(statements[1].sql).toContain("analysis_ref = ? AND attempts = ?");
    expect(statements[1].args).toEqual([
      "a".repeat(40),
      "2026-07-20T00:00:00.000Z",
      "22222222-2222-4222-8222-222222222222",
      "a".repeat(40),
      2,
    ]);
  });

  it.each([0, 2])(
    "returns false unless the claim changes exactly one row (changes=%i)",
    async (claimChanges) => {
      const { db, batch } = fakeDatabase(claimChanges);
      const store = new D1RelayStore(db);

      await expect(
        store.setAnalysisRef(
          "22222222-2222-4222-8222-222222222222",
          "dev",
          1,
          "2026-07-20T00:00:00.000Z",
        ),
      ).resolves.toBe(false);

      const statements = batch.mock.calls[0][0] as FakeStatement[];
      expect(statements[1].sql).toContain("WHERE changes() = 1");
    },
  );
});
