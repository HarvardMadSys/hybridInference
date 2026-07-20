import { describe, expect, it, vi } from "vitest";

import { GitHubApiClient, resolveAnalysisRef, type GitHubGateway } from "../src/github";

describe("trusted analysis refs", () => {
  it("uses a staging SHA only when GitHub proves it is on dev", async () => {
    const github = { isAncestor: vi.fn(async () => true) };
    await expect(resolveAnalysisRef("staging", "A".repeat(40), github)).resolves.toBe(
      "a".repeat(40),
    );
    expect(github.isAncestor).toHaveBeenCalledWith("a".repeat(40), "dev");

    github.isAncestor.mockResolvedValue(false);
    await expect(resolveAnalysisRef("staging", "b".repeat(40), github)).resolves.toBe("dev");
    await expect(resolveAnalysisRef("staging", "invalid", github)).resolves.toBe("dev");
  });

  it("always falls local analysis back to dev", async () => {
    const github = { isAncestor: vi.fn(async () => true) };
    await expect(resolveAnalysisRef("local", "a".repeat(40), github)).resolves.toBe("dev");
    await expect(resolveAnalysisRef("local", undefined, github)).resolves.toBe("dev");
    expect(github.isAncestor).not.toHaveBeenCalled();
  });

  it("uses a production SHA only when it is full-length and proven on main", async () => {
    const ancestor = { isAncestor: vi.fn(async () => true) };
    await expect(resolveAnalysisRef("production", "A".repeat(40), ancestor)).resolves.toBe(
      "a".repeat(40),
    );
    await expect(resolveAnalysisRef("production", "abc123", ancestor)).resolves.toBe("main");
    expect(ancestor.isAncestor).toHaveBeenCalledWith("a".repeat(40), "main");
  });

  it("falls back production to main on divergence or API failure", async () => {
    await expect(
      resolveAnalysisRef("production", "b".repeat(40), {
        isAncestor: async () => false,
      }),
    ).resolves.toBe("main");
    await expect(
      resolveAnalysisRef("production", "b".repeat(40), {
        isAncestor: async () => {
          throw new Error("offline");
        },
      }),
    ).resolves.toBe("main");
  });
});

describe("GitHub workflow API", () => {
  it("proves ancestry against the requested branch and dispatches job_id-only", async () => {
    const requests: Array<{ url: string; init: RequestInit }> = [];
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ url: String(input), init: init ?? {} });
      if (String(input).includes("/compare/")) {
        return Response.json({ status: "ahead" });
      }
      return new Response(null, { status: 204 });
    });
    const github: GitHubGateway = new GitHubApiClient(
      "github-secret",
      "org/repo",
      "codex-oncall-v2.yml",
      "https://api.github.test",
      fetcher as typeof fetch,
    );

    await expect(github.isAncestor("c".repeat(40), "dev")).resolves.toBe(true);
    await github.dispatch("22222222-2222-4222-8222-222222222222", "main");

    expect(requests[0].url).toContain(`/compare/${"c".repeat(40)}...dev`);
    expect(JSON.parse(String(requests[1].init.body))).toEqual({
      ref: "main",
      inputs: { job_id: "22222222-2222-4222-8222-222222222222" },
    });
    expect(String(requests[1].init.body)).not.toContain("alert");
  });
});
