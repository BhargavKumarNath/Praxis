// Formatting for an instrument: units always explicit, UTC always, no invented precision.

const nf = (digits: number) =>
  new Intl.NumberFormat("en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits });

export function integer(n: number): string {
  return nf(0).format(n);
}

export function decimal(n: number, digits = 2): string {
  return nf(digits).format(n);
}

export function compact(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${decimal(n / 1e9, 1)}B`;
  if (abs >= 1e6) return `${decimal(n / 1e6, 1)}M`;
  if (abs >= 1e4) return `${decimal(n / 1e3, 1)}K`;
  if (abs >= 1e3) return integer(n);
  return Number.isInteger(n) ? integer(n) : decimal(n, abs < 10 ? 2 : 1);
}

/** Integer minor units of a currency, e.g. 151348739 GBP minor -> "£1,513,487.39". */
export function money(minor: number, currency = "GBP"): string {
  return new Intl.NumberFormat("en-GB", { style: "currency", currency }).format(minor / 100);
}

export function moneyCompact(minor: number, currency = "GBP"): string {
  const symbol = currency === "GBP" ? "£" : `${currency} `;
  return `${symbol}${compact(minor / 100)}`;
}

/** Integer micros of GBP per billing unit -> "£0.0132": all significant micro digits, at
 * least two decimals (prices are set to the micro, so nothing is rounded away). */
export function micros(m: number): string {
  const s = (Math.abs(m) / 1e6).toFixed(6).replace(/0{1,4}$/, "");
  return `${m < 0 ? "−" : ""}£${s}`;
}

/** Signed money with the sign before the symbol: "+£500", "−£1,500". */
export function signedMoney(pounds: number, digits = 0): string {
  const s = `£${decimal(Math.abs(pounds), digits)}`;
  return pounds > 0 ? `+${s}` : pounds < 0 ? `−${s}` : s;
}

export function percent(ratio: number, digits = 1): string {
  return `${decimal(ratio * 100, digits)}%`;
}

export function signed(n: number, digits = 1): string {
  const s = decimal(Math.abs(n), digits);
  return n > 0 ? `+${s}` : n < 0 ? `−${s}` : s;
}

/** "3h 12m", "4m 05s", "2d 3h": the age of something, in the largest two units. */
export function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}

export function utc(iso: string | null | undefined): string {
  if (!iso) return "not recorded";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export function utcTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : `${d.toISOString().slice(11, 19)} UTC`;
}

export function day(iso: string): string {
  const d = new Date(`${iso.slice(0, 10)}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
}

export function secondsBetween(fromIso: string, to: Date): number {
  return (to.getTime() - new Date(fromIso).getTime()) / 1000;
}

/** A snake_case code to words: "price_volatility" -> "price volatility". */
export function words(code: string): string {
  return code.replace(/[_:]+/g, " ").trim();
}

export function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const s = [...values].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? (s[mid] as number) : ((s[mid - 1] as number) + (s[mid] as number)) / 2;
}
