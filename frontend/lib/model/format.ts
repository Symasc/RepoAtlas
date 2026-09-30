const COUNT = new Intl.NumberFormat("en-US");
const RELATIVE = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const ABSOLUTE = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" });

export function formatCount(value: number): string {
  return COUNT.format(value);
}

/** "1 file", "3,821 files". */
export function plural(value: number, one: string, many = `${one}s`): string {
  return `${formatCount(value)} ${value === 1 ? one : many}`;
}

export function shortSha(sha: string | null): string | null {
  return sha === null ? null : sha.slice(0, 7);
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
];

/** "3 hours ago"; under a minute reads "just now". */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const seconds = Math.round((Date.parse(iso) - now) / 1000);
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return RELATIVE.format(Math.round(seconds / size), unit);
  }
  return "just now";
}

export function absoluteTime(iso: string): string {
  return ABSOLUTE.format(new Date(iso));
}
