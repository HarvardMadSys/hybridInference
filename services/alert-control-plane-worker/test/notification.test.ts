import { describe, expect, it } from "vitest";

import {
  DeliveryRefValidationError,
  deliveryRefEquals,
  parseDeliveryRef,
  parseNotificationReceipt,
} from "../src/notification";

function validRef(): Record<string, unknown> {
  return {
    schemaVersion: 1,
    sinkId: "slack-primary",
    platform: "slack",
    destinationId: "C123",
    messageId: "100.001",
    conversationId: "100.001",
  };
}

describe("parseDeliveryRef", () => {
  it("accepts a valid reference with and without conversationId", () => {
    expect(parseDeliveryRef(validRef())).toEqual({
      schemaVersion: 1,
      sinkId: "slack-primary",
      platform: "slack",
      destinationId: "C123",
      messageId: "100.001",
      conversationId: "100.001",
    });

    const threadless = validRef();
    delete threadless.conversationId;
    const parsed = parseDeliveryRef(threadless);
    expect(parsed).toEqual({
      schemaVersion: 1,
      sinkId: "slack-primary",
      platform: "slack",
      destinationId: "C123",
      messageId: "100.001",
    });
    expect("conversationId" in parsed).toBe(false);
  });

  it.each([null, undefined, 42, "ref", ["ref"]])(
    "rejects non-object input %s",
    (value) => {
      expect(() => parseDeliveryRef(value)).toThrow(DeliveryRefValidationError);
    },
  );

  it("rejects an unsupported platform", () => {
    expect(() => parseDeliveryRef({ ...validRef(), platform: "discord" })).toThrow(
      /platform is unsupported/,
    );
  });

  it("rejects a wrong schemaVersion", () => {
    expect(() => parseDeliveryRef({ ...validRef(), schemaVersion: 2 })).toThrow(
      /schemaVersion must be 1/,
    );
  });

  it("rejects an unknown extra key", () => {
    expect(() => parseDeliveryRef({ ...validRef(), threadTs: "1" })).toThrow(
      /unsupported field: threadTs/,
    );
  });

  it.each(["sinkId", "destinationId", "messageId"])(
    "rejects a missing required field %s",
    (field) => {
      const input = validRef();
      delete input[field];
      expect(() => parseDeliveryRef(input)).toThrow(DeliveryRefValidationError);
    },
  );

  it.each(["sinkId", "destinationId", "messageId", "conversationId"])(
    "rejects an empty field %s",
    (field) => {
      expect(() => parseDeliveryRef({ ...validRef(), [field]: "" })).toThrow(
        /must not be empty/,
      );
    },
  );

  it.each(["sinkId", "destinationId", "messageId", "conversationId"])(
    "rejects an oversized field %s",
    (field) => {
      expect(() => parseDeliveryRef({ ...validRef(), [field]: "x".repeat(257) })).toThrow(
        /at most 256 characters/,
      );
    },
  );

  it.each(["sinkId", "destinationId", "messageId", "conversationId"])(
    "rejects a control character in field %s",
    (field) => {
      expect(() => parseDeliveryRef({ ...validRef(), [field]: "a\u0000b" })).toThrow(
        /control characters/,
      );
    },
  );

  it("rejects a null conversationId (optional but not nullable)", () => {
    expect(() => parseDeliveryRef({ ...validRef(), conversationId: null })).toThrow(
      DeliveryRefValidationError,
    );
  });

  it("rejects a non-string required field", () => {
    expect(() => parseDeliveryRef({ ...validRef(), messageId: 100 })).toThrow(
      /must be a string/,
    );
  });

  it.each([
    ["a Slack bot token", "xoxb-1234567890abcdef"],
    ["a Slack webhook URL", "https://hooks.slack.com/services/T000/B000/xxxx"],
    ["an authorization header", "Authorization: Bearer abcdefghijklmnop"],
    ["a github PAT", "github_pat_ABCDEFGHIJ0123456789"],
    ["a bare bearer token", "Bearer abcdefghijklmnop"],
    ["a hybrid inference key", "hyi-abcdefghijklmnopqrstuvwxyz"],
    ["any URL scheme", "slack://channel/C123"],
    ["an OpenAI-style key", "sk-test-NOTAREAL"],
    ["a Groq-style key", "gsk_NOTAREAL"],
    ["an xAI-style key", "xai-NOTAREAL"],
    ["an rk-style key", "rk_NOTAREAL"],
    ["a labeled API key", "api_key=NOTAREAL"],
    ["a labeled authorization value", "authorization=NOTAREAL"],
    ["a labeled cookie", "cookie=NOTAREAL"],
    ["a labeled password", "password=NOTAREAL"],
    ["a labeled secret", "secret=NOTAREAL"],
    ["a labeled token", "token=NOTAREAL"],
  ])("rejects %s as an opaque identifier", (_label, secret) => {
    expect(() => parseDeliveryRef({ ...validRef(), destinationId: secret })).toThrow(
      /opaque identifier/,
    );
    expect(() => parseDeliveryRef({ ...validRef(), messageId: secret })).toThrow(
      /opaque identifier/,
    );
  });

  it("still accepts realistic opaque Slack identifiers", () => {
    expect(
      parseDeliveryRef({
        schemaVersion: 1,
        sinkId: "slack-primary",
        platform: "slack",
        destinationId: "C0123ABCDEF",
        messageId: "1620000000.000100",
        conversationId: "1620000000.000100",
      }).messageId,
    ).toBe("1620000000.000100");
  });
});

describe("parseNotificationReceipt", () => {
  it("accepts and normalizes realistic Slack parent and reply IDs", () => {
    const parsed = parseNotificationReceipt({
      deliveryRef: {
        ...validRef(),
        messageId: "1620000000.000100",
        conversationId: "1620000000.000100",
      },
      externalEffectId: "1620000001.000200",
    });

    expect(parsed).toEqual({
      deliveryRef: {
        schemaVersion: 1,
        sinkId: "slack-primary",
        platform: "slack",
        destinationId: "C123",
        messageId: "1620000000.000100",
        conversationId: "1620000000.000100",
      },
      externalEffectId: "1620000001.000200",
    });
  });

  it("rejects unknown receipt fields", () => {
    expect(() =>
      parseNotificationReceipt({
        deliveryRef: validRef(),
        responseBody: "ok",
      }),
    ).toThrow(/unsupported field: responseBody/);
  });

  it.each([null, undefined, 42, true, {}, []])(
    "rejects a non-string externalEffectId: %s",
    (externalEffectId) => {
      expect(() =>
        parseNotificationReceipt({ deliveryRef: validRef(), externalEffectId }),
      ).toThrow(/externalEffectId is invalid/);
    },
  );

  it.each([
    "xoxb-1234567890abcdef",
    "api_key=NOTAREAL",
    "https://slack.com/api/chat.postMessage",
    "reply\u0000id",
    "x".repeat(257),
  ])("rejects an unsafe externalEffectId: %s", (externalEffectId) => {
    expect(() =>
      parseNotificationReceipt({ deliveryRef: validRef(), externalEffectId }),
    ).toThrow(/externalEffectId is invalid/);
  });
});

describe("deliveryRefEquals", () => {
  it("is true for identical references", () => {
    expect(deliveryRefEquals(parseDeliveryRef(validRef()), parseDeliveryRef(validRef()))).toBe(
      true,
    );
  });

  it("is false when the messageId differs", () => {
    expect(
      deliveryRefEquals(
        parseDeliveryRef(validRef()),
        parseDeliveryRef({ ...validRef(), messageId: "999.999" }),
      ),
    ).toBe(false);
  });

  it("distinguishes conversationId presence from absence", () => {
    const withThread = parseDeliveryRef(validRef());
    const threadlessInput = validRef();
    delete threadlessInput.conversationId;
    const withoutThread = parseDeliveryRef(threadlessInput);

    expect(deliveryRefEquals(withThread, withoutThread)).toBe(false);
    expect(deliveryRefEquals(withoutThread, withoutThread)).toBe(true);
  });
});
