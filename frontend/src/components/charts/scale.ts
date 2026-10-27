// Small scale helpers for the hand-built SVG charts (no chart library: the grammar is fixed
// in docs/design/dashboard.md and a few dozen lines cover it).

export function extent(values: number[]): [number, number] {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (Number.isFinite(v)) {
      lo = Math.min(lo, v);
      hi = Math.max(hi, v);
    }
  }
  if (lo === Infinity) return [0, 1];
  if (lo === hi) {
    const pad = Math.abs(lo) * 0.05 || 1;
    return [lo - pad, hi + pad];
  }
  return [lo, hi];
}

/** 3 to 5 ticks on clean numbers (1, 2, 2.5, 5 x 10^k) covering [lo, hi]. */
export function niceTicks(lo: number, hi: number, target = 4): number[] {
  const span = hi - lo;
  const raw = span / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= target + 1) ?? 10 * mag;
  const start = Math.floor(lo / step) * step;
  const ticks: number[] = [];
  for (let t = start; t <= hi + step * 0.5; t += step) {
    ticks.push(Number(t.toFixed(10)));
  }
  return ticks;
}

export function linear(d0: number, d1: number, r0: number, r1: number) {
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  return (v: number) => r0 + (v - d0) * k;
}

export const DAY_MS = 86_400_000;

export function dayMs(iso: string): number {
  return Date.parse(`${iso.slice(0, 10)}T00:00:00Z`);
}

/** Split points into runs: a missing value or a gap of more than one day breaks the line. */
export function runs<T extends { x: number; y: number | null }>(points: T[], maxGap = DAY_MS * 1.5): T[][] {
  const out: T[][] = [];
  let current: T[] = [];
  let prev: T | null = null;
  for (const p of points) {
    const broken = p.y === null || (prev !== null && p.x - prev.x > maxGap);
    if (broken && current.length) {
      out.push(current);
      current = [];
    }
    if (p.y !== null) current.push(p);
    prev = p;
  }
  if (current.length) out.push(current);
  return out;
}
