"use client";

// Read a console endpoint, refresh it on an interval, and keep the last good render on screen
// (dimmed) while a refresh runs or after one fails. The age of the last good read is part of
// the state, so a view can mark itself stale instead of silently showing old numbers.

import { useCallback, useEffect, useRef, useState } from "react";

import { request, type Result } from "./api";
import type { ApiError } from "./types";

export const REFRESH_MS = 30_000;

export interface Resource<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  readAt: Date | null; // time of the last successful read
  correlationId: string | null;
  reload: () => void;
}

interface Read<T> {
  path: string;
  data: T;
  at: Date;
  correlationId: string | null;
}

export function useResource<T>(path: string, refreshMs: number = REFRESH_MS): Resource<T> {
  // Everything is keyed by the path it was read from: after a path change the old
  // resource's data or error is never shown under the new one.
  const [read, setRead] = useState<Read<T> | null>(null);
  const [failure, setFailure] = useState<{ path: string; error: ApiError } | null>(null);
  const [settled, setSettled] = useState<string | null>(null); // path of the last finished read
  const [refreshing, setRefreshing] = useState(false);
  const seq = useRef(0);

  const fetchInto = useCallback(
    (p: string) => {
      const mine = ++seq.current;
      return request<T>(p).then((result: Result<T>) => {
        if (mine !== seq.current) return; // a newer request superseded this one
        if (result.ok) {
          setRead({ path: p, data: result.data, at: new Date(), correlationId: result.correlationId });
          setFailure(null);
        } else {
          setFailure({ path: p, error: result.error });
        }
        setSettled(p);
        setRefreshing(false);
      });
    },
    [],
  );

  const refresh = useCallback(() => {
    setRefreshing(true);
    void fetchInto(path);
  }, [fetchInto, path]);

  useEffect(() => {
    void fetchInto(path);
    if (refreshMs <= 0) return undefined;
    const id = window.setInterval(refresh, refreshMs);
    return () => window.clearInterval(id);
  }, [fetchInto, path, refresh, refreshMs]);

  const current = read && read.path === path ? read : null;
  return {
    data: current?.data ?? null,
    error: failure && failure.path === path ? failure.error : null,
    loading: refreshing || settled !== path,
    readAt: current?.at ?? null,
    correlationId: current?.correlationId ?? null,
    reload: refresh,
  };
}

/** Re-render every `ms` so ages ("12 s old") stay true without refetching. */
export function useNow(ms = 1000): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), ms);
    return () => window.clearInterval(id);
  }, [ms]);
  return now;
}
