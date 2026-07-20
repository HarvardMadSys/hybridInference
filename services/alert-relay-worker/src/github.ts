import type { TrustedEnvironment } from "./types";

const FULL_SHA_RE = /^[a-f0-9]{40}$/i;

export interface GitHubGateway {
  isAncestor(sha: string, branch: "dev" | "main"): Promise<boolean>;
  dispatch(jobId: string, workflowRef: "dev" | "main"): Promise<void>;
}

export function analysisBranch(environment: TrustedEnvironment): "dev" | "main" {
  return environment === "production" ? "main" : "dev";
}

export async function resolveAnalysisRef(
  environment: TrustedEnvironment,
  deploymentSha: string | undefined,
  github: Pick<GitHubGateway, "isAncestor">,
): Promise<string> {
  const branch = analysisBranch(environment);
  if (environment === "local" || !deploymentSha || !FULL_SHA_RE.test(deploymentSha)) {
    return branch;
  }
  const normalized = deploymentSha.toLowerCase();
  try {
    return (await github.isAncestor(normalized, branch)) ? normalized : branch;
  } catch {
    return branch;
  }
}

export class GitHubApiClient implements GitHubGateway {
  constructor(
    private readonly token: string,
    private readonly repository: string,
    private readonly workflowFile: string,
    private readonly apiBaseUrl = "https://api.github.com",
    private readonly fetcher: typeof fetch = fetch,
  ) {}

  private headers(): Record<string, string> {
    return {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${this.token}`,
      "Content-Type": "application/json",
      "X-GitHub-Api-Version": "2022-11-28",
    };
  }

  async isAncestor(sha: string, branch: "dev" | "main"): Promise<boolean> {
    if (!FULL_SHA_RE.test(sha)) return false;
    const url =
      `${this.apiBaseUrl.replace(/\/+$/, "")}/repos/${this.repository}/compare/` +
      `${encodeURIComponent(sha)}...${branch}`;
    let response: Response;
    try {
      response = await this.fetcher(url, {
        method: "GET",
        headers: this.headers(),
        redirect: "error",
        signal: AbortSignal.timeout(10_000),
      });
    } catch {
      return false;
    }
    if (!response.ok) return false;
    let body: { status?: unknown };
    try {
      body = (await response.json()) as { status?: unknown };
    } catch {
      return false;
    }
    return body.status === "ahead" || body.status === "identical";
  }

  async dispatch(jobId: string, workflowRef: "dev" | "main"): Promise<void> {
    const url =
      `${this.apiBaseUrl.replace(/\/+$/, "")}/repos/${this.repository}/actions/workflows/` +
      `${encodeURIComponent(this.workflowFile)}/dispatches`;
    let response: Response;
    try {
      response = await this.fetcher(url, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify({
          ref: workflowRef,
          inputs: { job_id: jobId },
        }),
        redirect: "error",
        signal: AbortSignal.timeout(15_000),
      });
    } catch {
      throw new Error("GitHub workflow dispatch request failed");
    }
    if (response.status !== 204) {
      throw new Error(`GitHub workflow dispatch returned HTTP ${response.status}`);
    }
  }
}
