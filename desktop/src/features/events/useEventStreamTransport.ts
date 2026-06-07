import { useEffect, useState } from "react";

import type { ApiClient } from "../../api/client";
import type { DesktopEventPayload, EventStreamConnectionState } from "../../types/api";
import { normalizeEventPayload } from "./eventStreamModel";

interface UseEventStreamTransportArgs {
  apiClient: ApiClient | null;
  baseUrl: string;
  onLog: (level: string, message: string) => void;
  onOpen: () => void;
  onPayload: (payload: DesktopEventPayload) => void;
}

function buildEventStreamUrl(baseUrl: string, token: string): string {
  const normalizedBase = baseUrl.trim().replace(/\/+$/, "");
  const url = new URL(`${normalizedBase}/api/events/stream`);
  const normalizedToken = token.trim();
  if (normalizedToken) {
    url.searchParams.set("token", normalizedToken);
  }
  return url.toString();
}

export function useEventStreamTransport({
  apiClient,
  baseUrl,
  onLog,
  onOpen,
  onPayload,
}: UseEventStreamTransportArgs): EventStreamConnectionState {
  const [streamState, setStreamState] = useState<EventStreamConnectionState>("idle");

  useEffect(() => {
    if (!apiClient || !baseUrl.trim()) {
      setStreamState("idle");
      return;
    }

    let closed = false;
    let eventSource: EventSource | null = null;
    let reconnectTimer: number | null = null;
    setStreamState("connecting");

    const closeEventSource = () => {
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
    };

    const queueReconnect = () => {
      if (closed) {
        return;
      }
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
      }
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        void connect();
      }, 1000);
    };

    const connect = async (): Promise<void> => {
      if (closed) {
        return;
      }
      setStreamState("connecting");

      let queryToken = "";
      if (apiClient.usesDesktopAuth) {
        try {
          const response = await apiClient.createDesktopQueryToken({ scope: "events" });
          queryToken = response.token;
        } catch {
          if (!closed) {
            setStreamState("error");
            onLog("warn", "event stream token request failed");
            queueReconnect();
          }
          return;
        }
      }

      if (closed) {
        return;
      }

      closeEventSource();
      eventSource = new EventSource(buildEventStreamUrl(baseUrl, queryToken));

      eventSource.onopen = () => {
        if (closed) {
          return;
        }
        setStreamState("open");
        onOpen();
      };

      eventSource.onerror = () => {
        if (closed) {
          return;
        }
        setStreamState("error");
        onLog("warn", "event stream reconnecting...");
        closeEventSource();
        queueReconnect();
      };

      eventSource.onmessage = (event) => {
        if (closed) {
          return;
        }

        let parsedPayload: unknown = null;
        try {
          parsedPayload = JSON.parse(event.data) as unknown;
        } catch {
          onLog("error", "invalid event payload");
          return;
        }

        const payload = normalizeEventPayload(parsedPayload);
        if (!payload) {
          return;
        }

        if (
          payload.event_type === "progress" ||
          payload.event_type === "log" ||
          payload.event_type === "state"
        ) {
          onPayload(payload);
        }
      };
    };

    void connect();

    return () => {
      closed = true;
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
      }
      closeEventSource();
    };
  }, [apiClient, baseUrl, onLog, onOpen, onPayload]);

  return streamState;
}
