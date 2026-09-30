import {
  ActionIcon,
  createTheme,
  Kbd,
  Popover,
  rem,
  Tooltip,
  type CSSVariablesResolver,
  type MantineColorsTuple,
} from "@mantine/core";

import classes from "./components.module.css";
import { cssVariables, DARK, LIGHT, type ColorTokens } from "./tokens";

const atlas: MantineColorsTuple = [
  "#eef3ff",
  "#dce6ff",
  "#b9cbff",
  "#93adff",
  "#7399ff",
  "#5b8cff",
  "#4a78f5",
  "#2f63f0",
  "#2450cc",
  "#1c3fa3",
];

/** Mantine's dark scale, retinted to the canvas's blue-black so its own surfaces agree. */
const dark: MantineColorsTuple = [
  "#e6edf7",
  "#c3cedd",
  "#9aa8bc",
  "#7a889c",
  "#2a3a4f",
  "#1f2c3d",
  "#152032",
  "#111b29",
  "#0c1420",
  "#070d16",
];

const gray: MantineColorsTuple = [
  "#f5f7fa",
  "#eef1f5",
  "#e2e7ee",
  "#cbd3de",
  "#a3aebd",
  "#7d8898",
  "#5f6c7e",
  "#475467",
  "#2b3646",
  "#0f1826",
];

export const theme = createTheme({
  primaryColor: "atlas",
  primaryShade: { light: 7, dark: 5 },
  colors: { atlas, dark, gray },
  white: "#ffffff",
  black: "#0f1826",
  autoContrast: true,
  respectReducedMotion: true,
  cursorType: "pointer",
  focusRing: "auto",
  fontFamily: "var(--font-geist-sans), ui-sans-serif, system-ui, sans-serif",
  fontFamilyMonospace: "var(--font-geist-mono), ui-monospace, SFMono-Regular, monospace",
  // 11 / 12 / 13 / 14 / 16, with 20 and 28 for headings; 13 is the body size
  fontSizes: { xs: rem(11), sm: rem(12), md: rem(13), lg: rem(14), xl: rem(16) },
  lineHeights: { xs: "1.4", sm: "1.45", md: "1.5", lg: "1.5", xl: "1.45" },
  headings: {
    fontFamily: "var(--font-geist-sans), ui-sans-serif, system-ui, sans-serif",
    fontWeight: "600",
    sizes: {
      h1: { fontSize: rem(28), lineHeight: "1.2" },
      h2: { fontSize: rem(20), lineHeight: "1.3" },
      h3: { fontSize: rem(16), lineHeight: "1.35" },
      h4: { fontSize: rem(14), lineHeight: "1.4" },
    },
  },
  // a 4px grid
  spacing: { xs: rem(4), sm: rem(8), md: rem(12), lg: rem(16), xl: rem(24) },
  radius: { xs: rem(4), sm: rem(6), md: rem(8), lg: rem(12), xl: rem(16) },
  defaultRadius: "sm",
  breakpoints: { xs: "36em", sm: "48em", md: "64em", lg: "80em", xl: "96em" },
  components: {
    ActionIcon: ActionIcon.extend({
      defaultProps: { variant: "subtle", color: "gray", size: "lg" },
      classNames: { root: classes.actionIcon },
    }),
    Tooltip: Tooltip.extend({
      defaultProps: { openDelay: 350, withinPortal: true },
      classNames: { tooltip: classes.tooltip },
    }),
    Popover: Popover.extend({
      defaultProps: { shadow: "md", radius: "md", offset: 8 },
      classNames: { dropdown: classes.dropdown },
    }),
    Kbd: Kbd.extend({ classNames: { root: classes.kbd } }),
  },
});

/** Points Mantine's own variables at the semantic tokens. */
function mantineVariables(t: ColorTokens): Record<string, string> {
  return {
    "--mantine-color-body": t.surfaceElevated,
    "--mantine-color-text": t.text,
    "--mantine-color-dimmed": t.textMuted,
    "--mantine-color-placeholder": t.textSubtle,
    "--mantine-color-anchor": t.accent,
    "--mantine-color-default": t.surface,
    "--mantine-color-default-hover": t.surfaceElevated,
    "--mantine-color-default-color": t.text,
    "--mantine-color-default-border": t.border,
  };
}

export const resolver: CSSVariablesResolver = () => ({
  variables: {
    "--ra-duration-fast": "120ms",
    "--ra-duration": "180ms",
    "--ra-duration-slow": "260ms",
    "--ra-ease": "cubic-bezier(0.2, 0.7, 0.2, 1)",
    "--ra-header-height": rem(52),
    "--ra-inspector-width": rem(400),
  },
  light: { ...cssVariables(LIGHT, "light"), ...mantineVariables(LIGHT) },
  dark: { ...cssVariables(DARK, "dark"), ...mantineVariables(DARK) },
});
