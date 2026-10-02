/**
 * The semantic colour tokens, one set per scheme, emitted as `--ra-<name>`.
 *
 * Neutrals are warm, like paper and graphite, not the blue-black of a default
 * dark theme. One accent, a vermilion, marks what is selected, focused or
 * pressed next, and nothing else: no chrome is tinted with it. A line's colour
 * is data, set as a transit map sets its lines: saturated and distinct, one per
 * relationship (imports blue, calls green, uses violet, other code links grey,
 * HTTP teal, package dependencies magenta, configuration amber), each at 4.5:1
 * or better on the canvas, and so is the text on a count tag filled with it. The canvas is
 * squared by a faint graticule, each repository is washed in its own hue
 * (see REPOSITORY_HUES), and a directory is a solid card a step above the
 * canvas, outlined, the same everywhere.
 * Light is designed on its own terms, not derived by inverting dark. Every text
 * tone keeps 4.5:1 on every background it sits on, tints included.
 */
export interface ColorTokens {
  canvas: string;
  /** The map's grid lines: a texture, never read. */
  graticule: string;
  /** What a repository's card on the overview is washed over. */
  groundRaised: string;
  /** A directory, in every repository: its icon and hover outline, its card's fill and outline. */
  directory: string;
  directoryFill: string;
  directoryBorder: string;
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
  /** Code links that are none of the relationships below: structure, mostly. */
  edgeCross: string;
  /** Text on a count tag filled with a line's colour. */
  onLine: string;
  /** Code links by relationship. */
  relImports: string;
  relCalls: string;
  relUses: string;
  shadow: string;
}

export const DARK: ColorTokens = {
  canvas: "#131312",
  graticule: "rgba(236, 233, 226, 0.055)",
  groundRaised: "#1a1a19",
  directory: "#b3aea4",
  directoryFill: "#232322",
  directoryBorder: "#5f5c56",
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
  package: "#ff6fae",
  endpoint: "#22c7c0",
  config: "#ffb43a",
  halo: "rgba(239, 122, 80, 0.28)",
  success: "#74c27a",
  warning: "#e2a447",
  danger: "#f2707f",
  evidence: "rgba(226, 196, 92, 0.13)",
  edge: "#45433f",
  edgeCross: "#a3a09a",
  onLine: "#131312",
  relImports: "#4f9dff",
  relCalls: "#3fcf73",
  relUses: "#b07cff",
  shadow: "0 1px 0 rgba(0, 0, 0, 0.3), 0 6px 20px rgba(0, 0, 0, 0.35)",
};

export const LIGHT: ColorTokens = {
  // nothing is paper-white: the brightest surface sits near 86% luminance,
  // so a long read in daylight does not glare
  canvas: "#e3e0d9",
  graticule: "rgba(28, 26, 23, 0.07)",
  groundRaised: "#f1efea",
  directory: "#46423b",
  directoryFill: "#f1efea",
  directoryBorder: "#8f8a7f",
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
  package: "#ad1263",
  endpoint: "#006b66",
  config: "#7f4f00",
  halo: "rgba(168, 56, 26, 0.2)",
  success: "#276b2c",
  warning: "#855000",
  danger: "#a51833",
  evidence: "#ecdb95",
  edge: "#b4afa4",
  edgeCross: "#57534c",
  onLine: "#ffffff",
  relImports: "#1c5bbd",
  relCalls: "#0f6a33",
  relUses: "#7139c7",
  shadow: "0 1px 2px rgba(28, 26, 23, 0.07), 0 6px 18px rgba(28, 26, 23, 0.08)",
};

/**
 * Repository identity hues, never the only signal: a repository is also its
 * frame and its name. Each is a swatch beside the name, and the wash over its
 * card on the overview. Lightness is set per scheme for contrast against
 * that scheme's surfaces; chroma is enough that a light wash still reads as a
 * colour.
 */
export const REPOSITORY_HUES = [255, 195, 150, 100, 65, 25, 340, 295] as const;

/** At light's lightness, teal, olive and amber hold less chroma inside sRGB than the rest. */
const LIGHT_CHROMA: Partial<Record<number, number>> = { 195: 0.078, 100: 0.095, 65: 0.104 };

export function repositoryColor(hue: number, scheme: "light" | "dark"): string {
  return scheme === "dark" ? `oklch(0.75 0.115 ${hue})` : `oklch(0.47 ${LIGHT_CHROMA[hue] ?? 0.12} ${hue})`;
}

/** How much of a repository's hue its card on the overview takes: every text tone keeps 4.5:1 on it. */
const WASH: Record<"light" | "dark", string> = { dark: "12%", light: "7%" };

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
  variables["--ra-wash-country"] = WASH[scheme];
  for (const [name, value] of Object.entries(CODE[scheme])) variables[`--ra-code-${name}`] = value;
  return variables;
}
