"use client";

import { useEffect, useRef } from "react";
import { useStore } from "./store";

// Prefer direct connection to backend in dev; fall back to relative /ws.
const WS_URL =
  typeof window !== "undefined"
    ? (() => {
        const proto = window.location.protocol === "https:" ? "wss" : "ws";
        const host = window.location.hostname;
        // Hard-code the backend port for now; Next.js dev proxy doesn't forward WS.
        return `${proto}://${host}:8765/ws`;
      })()
    : "";

/**
 * Connects to the edge backend's WebSocket and forwards every message into
 * the Zustand store. Auto-reconnects with exponential backoff.
 */
export function useEdgeWebSocket() {
  const setWsConnected = useStore((s) => s.setWsConnected);
  const ingest = useStore((s) => s.ingest);
  const retryRef = useRef(0);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let closed = false;

    const connect = () => {
      if (closed) return;
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        retryRef.current = 0;
        setWsConnected(true);
      };
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          ingest(msg);
        } catch (err) {
          console.warn("WS parse error", err);
        }
      };
      ws.onclose = () => {
        setWsConnected(false);
        if (closed) return;
        retryRef.current = Math.min(retryRef.current + 1, 6);
        const delay = Math.min(4000, 400 * 2 ** retryRef.current);
        setTimeout(connect, delay);
      };
      ws.onerror = () => {
        try {
          ws.close();
        } catch {}
      };
    };

    connect();
    return () => {
      closed = true;
      try {
        wsRef.current?.close();
      } catch {}
    };
  }, [setWsConnected, ingest]);
}
