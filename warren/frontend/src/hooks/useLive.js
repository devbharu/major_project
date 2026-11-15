import { useEffect, useRef } from "react";
import { api } from "../api/client";

// Subscribe to server-sent events. `topics` e.g. ["board", "pr:12"]; `onUpdate` fires (debounced) when anything changes.
// EventSource cannot send headers, so each connection first buys a 60-second single-use ticket (the session token never goes in a URL).
export function useLive(topics, onUpdate) {
  const callback = useRef(onUpdate);
  callback.current = onUpdate;
  const key = topics.filter(Boolean).join(",");

  useEffect(() => {
    if (!key || typeof EventSource === "undefined") return undefined;
    let source, timer, retry, closed = false, delay = 1000;

    const connect = async () => {
      try {
        const { ticket } = await api.streamTicket();
        if (closed) return;
        source = new EventSource(`/api/stream?topics=${encodeURIComponent(key)}&ticket=${encodeURIComponent(ticket)}`);
        source.onopen = () => { delay = 1000; };
        source.addEventListener("update", () => {
          clearTimeout(timer);
          timer = setTimeout(() => callback.current(), 250);
        });
        source.onerror = () => {            // tickets are single-use: reconnect with a fresh one instead of letting the browser retry
          source.close();
          if (!closed) retry = setTimeout(connect, (delay = Math.min(delay * 2, 30000)));
        };
      } catch {
        if (!closed) retry = setTimeout(connect, (delay = Math.min(delay * 2, 30000)));
      }
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      clearTimeout(retry);
      source?.close();
    };
  }, [key]);
}
