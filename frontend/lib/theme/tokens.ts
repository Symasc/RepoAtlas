/**
 * The semantic colour tokens, one set per scheme, emitted as `--ra-<name>`.
 *
 * Neutrals are warm, like paper and graphite, not the blue-black of a default
 * dark theme. One accent, a vermilion, marks what is selected, focused or
 * pressed next, and nothing else: no chrome is tinted with it. Code links are
 * drawn in ink; the other families of link keep a hue each (HTTP teal, package
 * dependencies plum, configuration ochre), because a line's colour is data.
 * Light is designed on its own terms, not derived by inverting dark. Every text
 * tone keeps 4.5:1 on every background it sits on, tints included.
 */
export interface ColorTokens {
  canvas: string;
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
  package: string;
  endpoint: string;
  config: string;
  halo: string;
  success: string;
  warning: string;
  danger: string;
  evidence: string;
  edge: string;
  /** Code links, and the mark a step into another repository carries. */
  edgeCross: string;
  shadow: string;
}

export const DARK: ColorTokens = {
  canvas: "#131312",
  surface: "#1a1a19",
  surfaceElevated: "#212120",
  border: "#2e2d2b",
  borderStrong: "#45433f",
  text: "#ece9e2",
  textMuted: "#b3aea4",
  textSubtle: "#9c978d",
  accent: "#ef7a50",
  accentStrong: "#ff9a74",
  onAccent: "#1b0d07",
  selected: "rgba(239, 122, 80, 0.16)",
  hover: "rgba(236, 233, 226, 0.06)",
  focus: "#ef7a50",
  package: "#c690bb",
  endpoint: "#5fbdb4",
  config: "#d3af52",
  halo: "rgba(239, 122, 80, 0.28)",
  success: "#74c27a",
  warning: "#e2a447",
  danger: "#f2707f",
  evidence: "rgba(226, 196, 92, 0.13)",
  edge: "#45433f",
  edgeCross: "#a29d93",
  shadow: "0 1px 0 rgba(0, 0, 0, 0.3), 0 6px 20px rgba(0, 0, 0, 0.35)",
};

export const LIGHT: ColorTokens = {
  // nothing is paper-white: the brightest surface sits near 86% luminance,
  // so a long read in daylight does not glare
  canvas: "#e3e0d9",
  surface: "#ece9e3",
  surfaceElevated: "#f1efea",
  border: "#d2cec5",
  borderStrong: "#b4afa4",
  text: "#1c1a17",
  textMuted: "#46423b",
  textSubtle: "#58534a",
  accent: "#a8381a",
  accentStrong: "#8f2d0f",
  onAccent: "#ffffff",
  selected: "rgba(168, 56, 26, 0.11)",
  hover: "rgba(28, 26, 23, 0.06)",
  focus: "#a8381a",
  package: "#7b3a6c",
  endpoint: "#0b6660",
  config: "#735400",
  halo: "rgba(168, 56, 26, 0.2)",
  success: "#276b2c",
  warning: "#855000",
  danger: "#a51833",
  evidence: "#ecdb95",
  edge: "#b4afa4",
  edgeCross: "#5a554c",
  shadow: "0 1px 2px rgba(28, 26, 23, 0.07), 0 6px 18px rgba(28, 26, 23, 0.08)",
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
 * Each tone keeps 4.5:1 on that scheme's code background, comments included,
 * and on an evidence line: code is text people read, not decoration.
 */
const CODE: Record<"light" | "dark", Record<string, string>> = {
  dark: {
    foreground: "#e2ded6",
    background: "#171716",
    "token-keyword": "#cf9bd0",
    "token-function": "#8fb8e8",
    "token-string": "#9bcf8f",
    "token-string-expression": "#d8b25a",
    "token-constant": "#eda46f",
    "token-parameter": "#ece9e2",
    "token-punctuation": "#a9a49a",
    "token-comment": "#9c978d",
    "token-link": "#66c2b9",
  },
  light: {
    foreground: "#1c1a17",
    background: "#e9e6df",
    "token-keyword": "#73306b",
    "token-function": "#1f4f8a",
    "token-string": "#2a6528",
    "token-string-expression": "#6d5000",
    "token-constant": "#8a3a08",
    "token-parameter": "#1c1a17",
    "token-punctuation": "#46423b",
    "token-comment": "#555048",
    "token-link": "#0b6058",
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
