// The shipped app reads every number from the console API: no app module may import test
// fixtures, and none may embed a fetch to anything but the console API base.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const SRC = join(__dirname, "..");

function appFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "test" ? [] : appFiles(path);
    return /\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : [];
  });
}

describe("app code", () => {
  const files = appFiles(SRC);

  it("exists", () => {
    expect(files.length).toBeGreaterThan(10);
  });

  it.each(files.map((f) => [f.slice(SRC.length + 1), f]))("%s imports no test fixture", (_name, path) => {
    const text = readFileSync(path, "utf8");
    expect(text).not.toMatch(/from\s+["'][^"']*\/test\//);
    expect(text).not.toMatch(/fixtures/);
  });

  it("reaches the network only through lib/api", () => {
    const net = /\b(fetch|XMLHttpRequest|EventSource|WebSocket)\b/;
    const fetching = files.filter((f) => net.test(readFileSync(f, "utf8")));
    expect(fetching.map((f) => f.slice(SRC.length + 1))).toEqual(["lib/api.ts"]);
  });
});
