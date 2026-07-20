import { authenticateProducer, authenticateWorkflow } from "./auth";
import { GitHubApiClient } from "./github";
import { AlertRelayService, RelayConflictError, RelayNotFoundError, RelayTemporaryError } from "./service";
import { SlackApiClient, SlackDeliveryError } from "./slack";
import { D1RelayStore } from "./store";
import type { Env, JobQueueMessage } from "./types";
import { parseAlertEvent, parseCompletion, ValidationError } from "./validation";

const MAX_BODY_BYTES = 128 * 1024;
const JOB_PATH_RE = /^\/v2\/jobs\/([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$/i;
const COMPLETE_PATH_RE =
  /^\/v2\/jobs\/([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\/complete$/i;

class ConfigurationError extends Error {}

function required(value: string | undefined, name: string): string {
  const normalized = value?.trim();
  if (!normalized) throw new ConfigurationError(`${name} is not configured`);
  return normalized;
}

function boundedConfig(value: string, name: string, maxLength: number): string {
  if (value.length > maxLength || /[\u0000-\u001f\u007f-\u009f]/.test(value)) {
    throw new ConfigurationError(`${name} is invalid`);
  }
  return value;
}

function configuredHttpsUrl(value: string, name: string): string {
  const bounded = boundedConfig(value, name, 2_000);
  let url: URL;
  try {
    url = new URL(bounded);
  } catch {
    throw new ConfigurationError(`${name} is invalid`);
  }
  if (
    url.protocol !== "https:" ||
    !url.hostname ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  ) {
    throw new ConfigurationError(`${name} is invalid`);
  }
  return url.toString().replace(/\/+$/, "");
}

function service(env: Env): AlertRelayService {
  const slackToken = required(env.SLACK_BOT_TOKEN, "SLACK_BOT_TOKEN");
  const slackChannelId = boundedConfig(
    required(env.SLACK_CHANNEL_ID, "SLACK_CHANNEL_ID"),
    "SLACK_CHANNEL_ID",
    128,
  );
  const githubToken = required(env.GITHUB_TOKEN, "GITHUB_TOKEN");
  const repository = boundedConfig(
    required(env.GITHUB_REPOSITORY, "GITHUB_REPOSITORY"),
    "GITHUB_REPOSITORY",
    200,
  );
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) {
    throw new ConfigurationError("GITHUB_REPOSITORY is invalid");
  }
  const workflowFile = boundedConfig(
    env.GITHUB_WORKFLOW_FILE?.trim() || "codex-oncall-v2.yml",
    "GITHUB_WORKFLOW_FILE",
    200,
  );
  const model = boundedConfig(required(env.CODEX_MODEL, "CODEX_MODEL"), "CODEX_MODEL", 200);
  const modelBaseUrl = configuredHttpsUrl(
    required(env.MODEL_BASE_URL, "MODEL_BASE_URL"),
    "MODEL_BASE_URL",
  );
  const github = new GitHubApiClient(
    githubToken,
    repository,
    workflowFile,
    configuredHttpsUrl(
      env.GITHUB_API_BASE_URL?.trim() || "https://api.github.com",
      "GITHUB_API_BASE_URL",
    ),
  );
  return new AlertRelayService(
    new D1RelayStore(env.DB),
    new SlackApiClient(slackToken, slackChannelId),
    env.ALERT_JOBS,
    github,
    {
      slackChannelId,
      model,
      modelBaseUrl,
    },
  );
}

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

async function body(request: Request): Promise<unknown> {
  const contentType = request.headers.get("content-type")?.split(";", 1)[0]?.trim();
  if (contentType !== "application/json") throw new ValidationError("content-type must be application/json");
  const declared = Number.parseInt(request.headers.get("content-length") ?? "0", 10);
  if (declared > MAX_BODY_BYTES) throw new ValidationError("request body is too large");
  const raw = await request.text();
  if (new TextEncoder().encode(raw).byteLength > MAX_BODY_BYTES) {
    throw new ValidationError("request body is too large");
  }
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    throw new ValidationError("request body must be valid JSON");
  }
}

function errorResponse(error: unknown): Response {
  if (error instanceof ValidationError) return json({ error: error.message }, 400);
  if (error instanceof RelayNotFoundError) return json({ error: "not found" }, 404);
  if (error instanceof RelayConflictError) return json({ error: error.message }, 409);
  if (error instanceof SlackDeliveryError) return json({ error: "Slack delivery failed" }, 502);
  if (error instanceof RelayTemporaryError || error instanceof ConfigurationError) {
    return json({ error: error.message }, 503);
  }
  console.error("alert relay request failed");
  return json({ error: "internal error" }, 500);
}

function readiness(env: Env): { status: "ok" | "unconfigured" | "misconfigured"; ready: boolean } {
  const producerTokens = [
    env.ALERT_RELAY_V2_STAGING_TOKEN?.trim(),
    env.ALERT_RELAY_V2_PRODUCTION_TOKEN?.trim(),
    env.ALERT_RELAY_V2_LOCAL_TOKEN?.trim(),
  ].filter((token): token is string => Boolean(token));
  const configured = Boolean(
    env.SLACK_BOT_TOKEN?.trim() &&
      env.SLACK_CHANNEL_ID?.trim() &&
      env.GITHUB_TOKEN?.trim() &&
      env.GITHUB_REPOSITORY?.trim() &&
      env.CODEX_MODEL?.trim() &&
      env.MODEL_BASE_URL?.trim() &&
      env.ALERT_RELAY_V2_WORKFLOW_TOKEN?.trim() &&
      producerTokens.length > 0,
  );
  if (!configured) return { status: "unconfigured", ready: false };
  if (new Set(producerTokens).size !== producerTokens.length) {
    return { status: "misconfigured", ready: false };
  }
  try {
    configuredHttpsUrl(env.GITHUB_API_BASE_URL?.trim() || "https://api.github.com", "GITHUB_API_BASE_URL");
    configuredHttpsUrl(required(env.MODEL_BASE_URL, "MODEL_BASE_URL"), "MODEL_BASE_URL");
  } catch {
    return { status: "misconfigured", ready: false };
  }
  return { status: "ok", ready: true };
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/healthz") {
      const health = readiness(env);
      return json(
        {
          ...health,
          version: "2",
        },
        health.status === "misconfigured" ? 503 : 200,
      );
    }

    try {
      if (request.method === "POST" && url.pathname === "/v2/alerts") {
        const environment = await authenticateProducer(request.headers.get("authorization"), env);
        if (!environment) return json({ error: "invalid producer token" }, 401);
        const event = parseAlertEvent(await body(request));
        return json(await service(env).submitAlert(event, environment), 202);
      }

      const jobMatch = request.method === "GET" ? JOB_PATH_RE.exec(url.pathname) : null;
      if (jobMatch) {
        if (!(await authenticateWorkflow(request.headers.get("authorization"), env))) {
          return json({ error: "invalid workflow token" }, 401);
        }
        return json(await service(env).getWorkflowJob(jobMatch[1]));
      }

      const completeMatch =
        request.method === "POST" ? COMPLETE_PATH_RE.exec(url.pathname) : null;
      if (completeMatch) {
        if (!(await authenticateWorkflow(request.headers.get("authorization"), env))) {
          return json({ error: "invalid workflow token" }, 401);
        }
        const completion = parseCompletion(await body(request));
        return json(await service(env).completeJob(completeMatch[1], completion), 202);
      }
      return json({ error: "not found" }, 404);
    } catch (error) {
      return errorResponse(error);
    }
  },

  async queue(batch: MessageBatch<JobQueueMessage>, env: Env): Promise<void> {
    const relay = service(env);
    for (const message of batch.messages) {
      try {
        const action = await relay.processQueueJob(message.body.job_id, message.attempts);
        if (action === "retry") {
          message.retry({ delaySeconds: Math.min(300, 2 ** Math.min(message.attempts, 8)) });
        } else {
          message.ack();
        }
      } catch {
        console.error("alert relay queue delivery failed");
        message.retry({ delaySeconds: Math.min(300, 2 ** Math.min(message.attempts, 8)) });
      }
    }
  },
} satisfies ExportedHandler<Env, JobQueueMessage>;
