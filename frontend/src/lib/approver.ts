"use client";

// The approver's bearer token for this browser tab. The SERVER decides who may approve: the
// token is checked in FastAPI (praxis.console.auth) and names the approver. The UI only
// carries it. It lives in sessionStorage (gone when the tab closes), never in localStorage.

import { useCallback, useEffect, useState } from "react";

import { authHeader, request, type Result } from "./api";
import type { ApiError } from "./types";

const KEY = "praxis.console.approverToken";

function read(): string | null {
  try {
    return window.sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

function write(token: string | null): void {
  try {
    if (token) window.sessionStorage.setItem(KEY, token);
    else window.sessionStorage.removeItem(KEY);
  } catch {
    // storage blocked: the token then lasts for this render only
  }
}

export interface Approver {
  token: string | null;
  name: string | null;
  error: ApiError | null;
  checking: boolean;
  signIn: (token: string) => Promise<boolean>;
  signOut: () => void;
}

export function useApprover(): Approver {
  const [token, setToken] = useState<string | null>(null);
  const [name, setName] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [checking, setChecking] = useState(false);

  // State changes only in the promise callback, never synchronously in the effect.
  const apply = useCallback((candidate: string, result: Result<{ approver: string }>) => {
    setChecking(false);
    if (result.ok) {
      setToken(candidate);
      setName(result.data.approver);
      setError(null);
      write(candidate);
      return true;
    }
    setToken(null);
    setName(null);
    setError(result.error);
    write(null);
    return false;
  }, []);

  const check = useCallback(
    (candidate: string) =>
      request<{ approver: string }>("/whoami", { headers: authHeader(candidate) }).then((r) =>
        apply(candidate, r),
      ),
    [apply],
  );

  useEffect(() => {
    const saved = read();
    if (saved) void check(saved);
  }, [check]);

  return {
    token,
    name,
    error,
    checking,
    signIn: (candidate: string) => {
      setChecking(true);
      return check(candidate);
    },
    signOut: () => {
      write(null);
      setToken(null);
      setName(null);
    },
  };
}
