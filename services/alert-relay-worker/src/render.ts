import type { Incident, OnCallAnalysis, SlackBlock, SlackMessage } from "./types";

const CONTROL_RE = /[\u0000-\u001f\u007f-\u009f]/g;
const MULTILINE_CONTROL_RE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g;
const FULL_SHA_RE = /^[a-f0-9]{40}$/i;

function bounded(value: string, limit: number): string {
  return value.length <= limit ? value : `${value.slice(0, Math.max(0, limit - 1))}…`;
}

function plain(value: string, limit = 150): string {
  return bounded(value.replace(CONTROL_RE, " ").trim(), limit);
}

/** Escape Slack entity/mention syntax after removing non-renderable controls. */
export function escapeSlack(value: string, limit = 3_000): string {
  const escaped = bounded(value.replace(MULTILINE_CONTROL_RE, " ").trim(), limit)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
  return bounded(escaped, limit);
}

function fallback(value: string, limit: number): string {
  return bounded(escapeSlack(value, limit).replaceAll("\n", " "), limit);
}

function mrkdwn(text: string): { type: "mrkdwn"; text: string } {
  return { type: "mrkdwn", text: bounded(text, 3_000) };
}

function field(label: string, value: string): { type: "mrkdwn"; text: string } {
  return { type: "mrkdwn", text: bounded(`*${label}*\n${value}`, 2_000) };
}

function environmentLabel(incident: Incident): string {
  return incident.environment.toUpperCase();
}

function deploymentIdentity(incident: Incident): string {
  const branch = incident.environment === "production" ? "main" : "dev";
  if (!incident.analysisRef) return `${branch}@pending`;
  if (FULL_SHA_RE.test(incident.analysisRef)) {
    return `${branch}@${incident.analysisRef.toLowerCase()}`;
  }
  return `${branch}@unavailable (analysis ref: ${incident.analysisRef})`;
}

function codexLabel(incident: Incident): string {
  if (incident.status === "resolved" || incident.codexStatus === "resolved") return "Resolved";
  if (incident.codexStatus === "analysis_ready") return "Analysis ready";
  if (incident.codexStatus === "unavailable") return "Unavailable";
  return "Investigating";
}

type ContextScalar = null | boolean | number | string;

function contextLabel(path: string[]): string {
  return path
    .map((part) =>
      part
        .replaceAll("_", " ")
        .replace(/\b\w/g, (letter) => letter.toUpperCase()),
    )
    .join(" · ");
}

function contextPriority(path: string[]): number {
  const key = path.at(-1)?.toLowerCase() ?? "";
  if (key.includes("error")) return 100;
  if (key === "provider") return 90;
  if (key === "model_id") return 80;
  if (key.includes("status")) return 70;
  if (key.includes("gateway")) return 60;
  return 10;
}

function scalarText(value: ContextScalar): string {
  if (value === null) return "null";
  return String(value);
}

function renderContextFields(incident: Incident): Array<{ type: "mrkdwn"; text: string }> {
  const values: Array<{ path: string[]; value: ContextScalar; order: number }> = [];
  let order = 0;
  for (const [key, value] of Object.entries(incident.alert.context)) {
    if (value === null || ["string", "number", "boolean"].includes(typeof value)) {
      values.push({ path: [key], value: value as ContextScalar, order: order++ });
      continue;
    }
    if (Array.isArray(value) || typeof value !== "object") continue;
    for (const [nestedKey, nestedValue] of Object.entries(value)) {
      if (
        nestedValue === null ||
        ["string", "number", "boolean"].includes(typeof nestedValue)
      ) {
        values.push({
          path: [key, nestedKey],
          value: nestedValue as ContextScalar,
          order: order++,
        });
      }
    }
  }
  return values
    .sort(
      (left, right) =>
        contextPriority(right.path) - contextPriority(left.path) || left.order - right.order,
    )
    .slice(0, 6)
    .map(({ path, value }) =>
      field(escapeSlack(contextLabel(path), 100), escapeSlack(scalarText(value), 500)),
    );
}

function outageDuration(incident: Incident): string {
  const started = Date.parse(incident.firstSeen);
  const ended = Date.parse(incident.lastSeen);
  if (!Number.isFinite(started) || !Number.isFinite(ended)) return "Unavailable";
  let seconds = Math.max(0, Math.floor((ended - started) / 1_000));
  const days = Math.floor(seconds / 86_400);
  seconds %= 86_400;
  const hours = Math.floor(seconds / 3_600);
  seconds %= 3_600;
  const minutes = Math.floor(seconds / 60);
  seconds %= 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

function failureCount(incident: Incident, final = false): string {
  const contexts = final
    ? [incident.resolutionAlert?.context, incident.alert.context]
    : [incident.alert.context];
  const keys = final
    ? ["final_failure_count", "failure_count", "consecutive_failures"]
    : ["failure_count", "consecutive_failures"];
  for (const context of contexts) {
    if (!context) continue;
    for (const key of keys) {
      const value = context[key];
      if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
        return String(Math.floor(value));
      }
    }
  }
  return String(incident.occurrenceCount);
}

function severityIcon(severity: string): string {
  if (severity === "critical") return "🚨";
  if (severity === "error") return "❌";
  if (severity === "warn") return "⚠️";
  return "ℹ️";
}

/** The single parent message, reused by initial, repeat, and resolved chat.update calls. */
export function renderParent(incident: Incident): SlackMessage {
  const resolved = incident.status === "resolved";
  const state = resolved ? "Resolved" : "Firing";
  const icon = resolved ? "✅" : severityIcon(incident.alert.severity);
  const environment = environmentLabel(incident);
  const fallbackText = bounded(
    `[${environment}] ${state.toUpperCase()} · ${fallback(incident.alert.title, 300)} · ` +
      `Incident ${incident.id} · Failures ${incident.occurrenceCount}`,
    4_000,
  );
  const contextFields = renderContextFields(incident);
  const blocks: SlackBlock[] = [
    {
      type: "header",
      text: {
        type: "plain_text",
        text: plain(`${icon} ${environment} · ${state}`, 150),
        emoji: true,
      },
    },
    {
      type: "section",
      text: mrkdwn(`*${escapeSlack(incident.alert.title, 500)}*\n${escapeSlack(incident.alert.summary)}`),
    },
    {
      type: "section",
      fields: [
        field("Environment", environment),
        field("Severity", escapeSlack(incident.alert.severity.toUpperCase(), 32)),
        field("Incident", escapeSlack(incident.id, 128)),
        field("Deployment", escapeSlack(deploymentIdentity(incident), 160)),
        field("First seen", escapeSlack(incident.firstSeen, 64)),
        field("Last seen", escapeSlack(incident.lastSeen, 64)),
        field("Failure count", failureCount(incident)),
        field("Codex", codexLabel(incident)),
      ],
    },
    ...(contextFields.length > 0 ? [{ type: "section" as const, fields: contextFields }] : []),
    {
      type: "context",
      elements: [
        mrkdwn(
          `Source: ${escapeSlack(incident.alert.source, 128)} · ` +
            `Fingerprint: ${escapeSlack(incident.fingerprint, 512)}`,
        ),
      ],
    },
  ];
  return { text: fallbackText, blocks };
}

export function renderRecoveryReply(incident: Incident): SlackMessage {
  const recovery = incident.resolutionAlert;
  const title = recovery?.title ?? incident.alert.title;
  const summary = recovery?.summary ?? "The incident recovered.";
  return {
    text: bounded(
      `[${environmentLabel(incident)}] RESOLVED · ${fallback(title, 300)} · Incident ${incident.id}`,
      4_000,
    ),
    blocks: [
      {
        type: "header",
        text: { type: "plain_text", text: plain("✅ Recovery confirmed"), emoji: true },
      },
      {
        type: "section",
        text: mrkdwn(`*${escapeSlack(title, 500)}*\n${escapeSlack(summary)}`),
      },
      {
        type: "section",
        fields: [
          field("Outage duration", outageDuration(incident)),
          field("Final failure count", failureCount(incident, true)),
        ],
      },
      {
        type: "context",
        elements: [
          mrkdwn(
            `Incident ${escapeSlack(incident.id, 128)} · ` +
              `Resolved ${escapeSlack(incident.lastSeen, 64)}`,
          ),
        ],
      },
    ],
  };
}

export function renderAnalysisReply(
  incident: Incident,
  analysis: OnCallAnalysis,
  codexThreadId?: string,
): SlackMessage {
  const evidence =
    analysis.evidence.length > 0
      ? analysis.evidence.map((item) => `• ${escapeSlack(item, 250)}`).join("\n")
      : "• None";
  const actions = analysis.recommended_actions
    .map((item, index) => `${index + 1}. ${escapeSlack(item, 300)}`)
    .join("\n");
  const threadLine = codexThreadId
    ? `\n*Codex thread:* ${escapeSlack(codexThreadId, 128)}`
    : "";
  return {
    text: bounded(
      `[${environmentLabel(incident)}] Codex analysis · ${fallback(analysis.summary, 500)} · ` +
        `Incident ${incident.id}`,
      4_000,
    ),
    blocks: [
      {
        type: "header",
        text: { type: "plain_text", text: "🔎 Codex analysis", emoji: true },
      },
      {
        type: "section",
        fields: [
          field("Classification", escapeSlack(analysis.classification, 64)),
          field("Confidence", `${Math.round(analysis.confidence * 100)}%`),
        ],
      },
      {
        type: "section",
        text: mrkdwn(`*Summary*\n${escapeSlack(analysis.summary, 2_900)}`),
      },
      {
        type: "section",
        fields: [
          field("Impact", escapeSlack(analysis.impact, 1_900)),
          field("Likely cause", escapeSlack(analysis.likely_cause, 1_900)),
        ],
      },
      { type: "section", text: mrkdwn(`*Evidence*\n${evidence}`) },
      { type: "section", text: mrkdwn(`*Recommended actions*\n${actions}`) },
      {
        type: "context",
        elements: [
          mrkdwn(
            `Issue recommendation: ${analysis.issue_recommendation} · ` +
              `Draft PR recommendation: ${analysis.draft_pr_recommendation}${threadLine}`,
          ),
        ],
      },
    ],
  };
}

export function renderUnavailableReply(
  incident: Incident,
  reason: string,
  runUrl?: string,
): SlackMessage {
  const runLine = runUrl ? `\n*Run URL:* ${escapeSlack(runUrl, 2_000)}` : "";
  return {
    text: bounded(
      `[${environmentLabel(incident)}] Codex unavailable · Incident ${incident.id}` +
        (runUrl ? ` · ${fallback(runUrl, 1_000)}` : ""),
      4_000,
    ),
    blocks: [
      {
        type: "header",
        text: { type: "plain_text", text: "⚠️ Codex analysis unavailable", emoji: true },
      },
      {
        type: "section",
        text: mrkdwn(
          `${escapeSlack(reason, 2_000)}${runLine}\n\nThe original incident remains authoritative.`,
        ),
      },
      {
        type: "context",
        elements: [mrkdwn(`Incident ${escapeSlack(incident.id, 128)}`)],
      },
    ],
  };
}
