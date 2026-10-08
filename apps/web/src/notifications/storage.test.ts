import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  HISTORY_LIMIT,
  loadCenter,
  saveCenter,
  type NotificationRecord,
} from "./storage";

function record(id: string): NotificationRecord {
  return {
    observedAt: "2026-10-09T00:00:00Z",
    notice: {
      id: `incident:${id}`,
      incidentId: id,
      title: `Incident ${id}`,
      kind: "incident",
      severity: "WARNING",
      service: null,
      diagnosed: false,
      resolution: null,
      confidence: null,
      leadingActor: null,
    },
  };
}

describe("browser notification history", () => {
  let values: Map<string, string>;
  beforeEach(() => {
    values = new Map();
    vi.stubGlobal("window", {
      localStorage: {
        getItem: (key: string) => values.get(key) ?? null,
        setItem: (key: string, value: string) => values.set(key, value),
      },
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("restores exact recorded notices and limits unread state to retained incidents", () => {
    const history = [record("one")];
    saveCenter({ history, unreadIds: ["one", "absent"] });
    expect(loadCenter()).toEqual({ history, unreadIds: ["one"] });
  });
  it("bounds restored history without inventing missing notifications", () => {
    saveCenter({
      history: Array.from({ length: 150 }, (_, i) => record(String(i))),
      unreadIds: [],
    });
    expect(loadCenter().history).toHaveLength(HISTORY_LIMIT);
  });
  it("ignores malformed storage and unavailable browser persistence", () => {
    values.set("agentic-sre.notification-center.v1", "not json");
    expect(loadCenter()).toEqual({ history: [], unreadIds: [] });
    values.set(
      "agentic-sre.notification-center.v1",
      JSON.stringify({
        history: [{ notice: null, observedAt: "unknown" }],
        unreadIds: [],
      }),
    );
    expect(loadCenter()).toEqual({ history: [], unreadIds: [] });
    vi.stubGlobal("window", {
      get localStorage() {
        throw new Error("unavailable");
      },
    });
    expect(() =>
      saveCenter({ history: [record("one")], unreadIds: ["one"] }),
    ).not.toThrow();
    expect(loadCenter()).toEqual({ history: [], unreadIds: [] });
  });
});
