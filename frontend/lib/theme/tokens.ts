/**
 * The semantic colour tokens, one set per scheme, emitted as `--ra-<name>`.
 *
 * Colour carries meaning: neutral inside a repository, the accent across one;
 * a family (package, endpoint) by its own hue; freshness by tone. Light is
 * designed on its own terms (white surfaces on a cool grey canvas, a highlighter
 * for evidence), not derived by inverting dark.
 */
export interface ColorTokens {
  canvas: string;
  gridDot: string;
  surface: string;
  surfaceElevated: string;
  border: string;
  borderStrong: string;
  text: string;
  textMuted: string;
  textSubtle: string;
  accent: string;
  accentStrong: string;
  onAccent: string;
  selected: string;
  hover: string;
  focus: string;
  repository: string;
  package: string;
  file: string;
  endpoint: string;
  external: string;
  config: string;
  halo: string;
  success: string;
  warning: string;
  danger: string;
  evidence: string;
  edge: string;
  edgeCross: string;
  shadow: string;
}

export const DARK: ColorTokens = {
  canvas: "#070d16",
  gridDot: "rgba(154, 168, 188, 0.22)",
  surface: "#0c1420",
  surfaceElevated: "#111b29",
  border: "#1c2838",
  borderStrong: "#2a3a4f",
  text: "#e6edf7",
  textMuted: "#9aa8bc",
  textSubtle: "#7a889c",
  accent: "#5b8cff",
  accentStrong: "#8aabff",
  onAccent: "#050a12",
  selected: "rgba(91, 140, 255, 0.16)",
  hover: "rgba(154, 168, 188, 0.08)",
  focus: "#8aabff",
  repository: "#c3cedd",
  package: "#8b73ff",
  file: "#9aa8bc",
  endpoint: "#38cfee",
  external: "#7a889c",
  config: "#e2b54f",
  halo: "rgba(91, 140, 255, 0.28)",
  success: "#2fd39f",
  warning: "#f2a33a",
  danger: "#f0616d",
  evidence: "rgba(242, 211, 90, 0.22)",
  edge: "#2a3a4f",
  edgeCross: "#5b8cff",
  shadow: "0 1px 0 rgba(0, 0, 0, 0.35), 0 12px 32px rgba(0, 0, 0, 0.45)",
};

export const LIGHT: ColorTokens = {
  canvas: "#f5f7fa",
  gridDot: "rgba(15, 24, 38, 0.16)",
  surface: "#ffffff",
  surfaceElevated: "#ffffff",
  border: "#e2e7ee",
  borderStrong: "#cbd3de",
  text: "#0f1826",
  textMuted: "#475467",
  textSubtle: "#5f6c7e",
  accent: "#2f63f0",
  accentStrong: "#1f4fd6",
  onAccent: "#ffffff",
  selected: "rgba(47, 99, 240, 0.1)",
  hover: "rgba(15, 24, 38, 0.05)",
  focus: "#2f63f0",
  repository: "#334155",
  package: "#6d4fe0",
  file: "#475467",
  endpoint: "#0b7f9e",
  external: "#5f6c7e",
  config: "#8f6400",
  halo: "rgba(47, 99, 240, 0.2)",
  success: "#0b7a57",
  warning: "#9a5a00",
  danger: "#c8313f",
  evidence: "#fff1a8",
  edge: "#cbd3de",
  edgeCross: "#2f63f0",
  shadow: "0 1px 2px rgba(15, 24, 38, 0.06), 0 12px 32px rgba(15, 24, 38, 0.1)",
};

/**
 * Repository identity hues, low in chroma so they label without shouting, and
 * never the only signal: a repository is also its frame and its name. Lightness
 * is set per scheme for contrast against that scheme's surfaces.
 */
export const REPOSITORY_HUES = [255, 195, 150, 100, 65, 25, 340, 295] as const;

export function repositoryColor(hue: number, scheme: "light" | "dark"): string {
  return scheme === "dark" ? `oklch(0.76 0.08 ${hue})` : `oklch(0.54 0.1 ${hue})`;
}

/**
 * Source colour, read by the code reader's highlighter as `--ra-code-<name>`.
 * Each tone keeps 4.5:1 on that scheme's code background, comments included:
 * code is text people read, not decoration.
 */
const CODE: Record<"light" | "dark", Record<string, string>> = {
  dark: {
    foreground: "#d9e2ef",
    background: "#0a111c",
    "token-keyword": "#a597ff",
    "token-function": "#7cabff",
    "token-string": "#7fd3a6",
    "token-string-expression": "#e2b54f",
    "token-constant": "#f2aa6b",
    "token-parameter": "#e6edf7",
    "token-punctuation": "#9aa8bc",
    "token-comment": "#7d8ba0",
    "token-link": "#38cfee",
  },
  light: {
    foreground: "#0f1826",
    background: "#ffffff",
    "token-keyword": "#6a3fd6",
    "token-function": "#1f56d6",
    "token-string": "#12795a",
    "token-string-expression": "#8f6400",
    "token-constant": "#a24a08",
    "token-parameter": "#0f1826",
    "token-punctuation": "#475467",
    "token-comment": "#5f6c7e",
    "token-link": "#0b7285",
  },
};

function kebab(name: string): string {
  return name.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
}

export function cssVariables(tokens: ColorTokens, scheme: "light" | "dark"): Record<string, string> {
  const variables: Record<string, string> = {};
  for (const [name, value] of Object.entries(tokens)) variables[`--ra-${kebab(name)}`] = value;
  REPOSITORY_HUES.forEach((hue, index) => {
    variables[`--ra-repo-${index + 1}`] = repositoryColor(hue, scheme);
  });
  for (const [name, value] of Object.entries(CODE[scheme])) variables[`--ra-code-${name}`] = value;
  return variables;
}
