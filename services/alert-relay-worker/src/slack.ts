import type { SlackMessage } from "./types";

export class SlackDeliveryError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SlackDeliveryError";
  }
}

export interface SlackWriter {
  postParent(message: SlackMessage, clientMessageId: string): Promise<string>;
  updateParent(threadTs: string, message: SlackMessage): Promise<void>;
  postReply(threadTs: string, message: SlackMessage, clientMessageId: string): Promise<void>;
}

interface SlackResponse {
  ok?: boolean;
  ts?: string;
  error?: string;
}

export class SlackApiClient implements SlackWriter {
  constructor(
    private readonly token: string,
    private readonly channelId: string,
    private readonly fetcher: typeof fetch = fetch,
  ) {}

  private async call(method: "chat.postMessage" | "chat.update", payload: object): Promise<SlackResponse> {
    let response: Response;
    try {
      response = await this.fetcher(`https://slack.com/api/${method}`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.token}`,
          "Content-Type": "application/json; charset=utf-8",
        },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(10_000),
      });
    } catch {
      throw new SlackDeliveryError(`Slack ${method} request failed`);
    }
    if (!response.ok) {
      throw new SlackDeliveryError(`Slack ${method} returned HTTP ${response.status}`);
    }
    let body: SlackResponse;
    try {
      body = (await response.json()) as SlackResponse;
    } catch {
      throw new SlackDeliveryError(`Slack ${method} returned invalid JSON`);
    }
    if (!body.ok) {
      throw new SlackDeliveryError(`Slack ${method} rejected the request: ${body.error ?? "unknown"}`);
    }
    return body;
  }

  async postParent(message: SlackMessage, clientMessageId: string): Promise<string> {
    const response = await this.call("chat.postMessage", {
      channel: this.channelId,
      text: message.text,
      blocks: message.blocks,
      client_msg_id: clientMessageId,
      unfurl_links: false,
      unfurl_media: false,
    });
    if (!response.ts) throw new SlackDeliveryError("Slack chat.postMessage omitted ts");
    return response.ts;
  }

  async updateParent(threadTs: string, message: SlackMessage): Promise<void> {
    await this.call("chat.update", {
      channel: this.channelId,
      ts: threadTs,
      text: message.text,
      blocks: message.blocks,
    });
  }

  async postReply(
    threadTs: string,
    message: SlackMessage,
    clientMessageId: string,
  ): Promise<void> {
    await this.call("chat.postMessage", {
      channel: this.channelId,
      thread_ts: threadTs,
      text: message.text,
      blocks: message.blocks,
      client_msg_id: clientMessageId,
      reply_broadcast: false,
      unfurl_links: false,
      unfurl_media: false,
    });
  }
}
