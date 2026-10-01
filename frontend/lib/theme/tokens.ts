/**
 * The semantic colour tokens, one set per scheme, emitted as `--ra-<name>`.
 *
 * Colour carries meaning: neutral inside a repository, the accent across one;
 * a family (package, endpoint) by its own hue; freshness by tone. Light is
 * designed on its own terms (soft grey surfaces a step above a cool grey canvas,
 * a highlighter for evidence), not derived by inverting dark. Every text tone
 * keeps 4.5:1 on every background it sits on, tints included.
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
  // nothing is paper-white: the brightest surface sits near 88% luminance,
  // so a long read in daylight does not glare
  canvas: "#dde2e9",
  gridDot: "rgba(16, 24, 38, 0.18)",
  surface: "#e8ecf1",
  surfaceElevated: "#eff2f5",
  border: "#cdd4de",
  borderStrong: "#b3bdca",
  text: "#101826",
  textMuted: "#3a4658",
  textSubtle: "#4a5668",
  accent: "#2150cc",
  accentStrong: "#1a44b8",
  onAccent: "#ffffff",
  selected: "rgba(37, 86, 216, 0.12)",
  hover: "rgba(16, 24, 38, 0.06)",
  focus: "#2150cc",
  repository: "#2c394b",
  package: "#5b3fcf",
  file: "#3a4658",
  endpoint: "#08657f",
  external: "#4a5668",
  config: "#765200",
  halo: "rgba(33, 80, 204, 0.22)",
  success: "#08664a",
  warning: "#7f4a00",
  danger: "#ad2633",
  evidence: "#f3e39a",
  edge: "#b3bdca",
  edgeCross: "#2150cc",
  shadow: "0 1px 2px rgba(16, 24, 38, 0.08), 0 10px 28px rgba(16, 24, 38, 0.1)",
};

/**
 * Repository identity hues, low in chroma so they label without shouting, and
 * never the only signal: a repository is also its frame and its name. Lightness
 * is set per scheme for contrast against that scheme's surfaces.
 */
export const REPOSITORY_HUES = [255, 195, 150, 100, 65, 25, 340, 295] as const;

export function repositoryColor(hue: number, scheme: "light" | "dark"): string {
  return scheme === "dark" ? `oklch(0.76 0.08 ${hue})` : `oklch(0.465 0.1 ${hue})`;
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
    foreground: "#101826",
    background: "#ecf0f4",
    "token-keyword": "#5a2fc4",
    "token-function": "#1a49b8",
    "token-string": "#0d664b",
    "token-string-expression": "#765200",
    "token-constant": "#8c3e05",
    "token-parameter": "#101826",
    "token-punctuation": "#3a4658",
    "token-comment": "#4f5b6c",
    "token-link": "#08617a",
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
