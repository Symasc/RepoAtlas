import {
  ActionIcon,
  createTheme,
  Kbd,
  Popover,
  rem,
  SegmentedControl,
  Switch,
  Tooltip,
  type CSSVariablesResolver,
  type MantineColorsTuple,
} from "@mantine/core";

import classes from "./components.module.css";
import { cssVariables, DARK, LIGHT, type ColorTokens } from "./tokens";

/** The accent: a vermilion, the red a map draws its route in. Shade 7 carries white text at 6.5:1. */
const atlas: MantineColorsTuple = [
  "#fdf0eb",
  "#fadccf",
  "#f4b8a0",
  "#ef9573",
  "#ef7a50",
  "#e0643a",
  "#c84d27",
  "#a8381a",
  "#8f2d0f",
  "#6e220a",
];

/** Mantine's dark scale, on the dark scheme's graphite so its own surfaces agree. */
const dark: MantineColorsTuple = [
  "#ece9e2",
  "#d8d4cb",
  "#b3aea4",
  "#9c978d",
  "#45433f",
  "#363532",
  "#2a2927",
  "#212120",
  "#1a1a19",
  "#131312",
];

/** Mantine's grey scale, on the light scheme's warm paper greys. */
const gray: MantineColorsTuple = [
  "#f1efea",
  "#e6e3dc",
  "#d2cec5",
  "#b4afa4",
  "#948e83",
  "#726c62",
  "#58534a",
  "#46423b",
  "#2d2a25",
  "#1c1a17",
];

/**
 * IBM Plex, self-hosted (app/layout.tsx imports it): a humanist sans for the
 * interface, and its mono for code, paths and identifiers, so the two agree.
 */
const SANS = "'IBM Plex Sans Variable', 'IBM Plex Sans', ui-sans-serif, system-ui, sans-serif";
const MONO = "'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace";

export const theme = createTheme({
  primaryColor: "atlas",
  primaryShade: { light: 7, dark: 7 },
  colors: { atlas, dark, gray },
  white: "#ffffff",
  black: "#1c1a17",
  autoContrast: true,
  // the reader's choice decides, through `data-motion` on the root (lib/motion.ts)
  respectReducedMotion: false,
  cursorType: "pointer",
  focusRing: "auto",
  fontFamily: SANS,
  fontFamilyMonospace: MONO,
  // 11 / 12 / 13 / 15 / 18, with 16, 20 and 28 for headings; 13 is the body
  // size. The steps are wide on purpose: a title is plainly not body text.
  fontSizes: { xs: rem(11), sm: rem(12), md: rem(13), lg: rem(15), xl: rem(18) },
  lineHeights: { xs: "1.4", sm: "1.45", md: "1.5", lg: "1.5", xl: "1.45" },
  headings: {
    fontFamily: SANS,
    fontWeight: "600",
    sizes: {
      h1: { fontSize: rem(28), lineHeight: "1.2" },
      h2: { fontSize: rem(20), lineHeight: "1.3" },
      h3: { fontSize: rem(16), lineHeight: "1.35" },
      h4: { fontSize: rem(15), lineHeight: "1.4" },
    },
  },
  // a 4px grid
  spacing: { xs: rem(4), sm: rem(8), md: rem(12), lg: rem(16), xl: rem(24) },
  // three corners and no more: 2 for small marks, 4 for controls and nodes,
  // 6 for what holds them; nothing is a pill
  radius: { xs: rem(2), sm: rem(4), md: rem(4), lg: rem(6), xl: rem(6) },
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
    // a plain slot and a square thumb, the theme's own colours
    Switch: Switch.extend({ defaultProps: { radius: "xs", withThumbIndicator: false } }),
    SegmentedControl: SegmentedControl.extend({
      defaultProps: { radius: "xs" },
      classNames: { root: classes.segmented, indicator: classes.segmentedIndicator, label: classes.segmentedLabel },
    }),
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
    "--ra-header-height": rem(48),
    "--ra-radius-s": rem(2),
    "--ra-radius": rem(4),
    "--ra-radius-l": rem(6),
    "--ra-inspector-width": rem(400),
  },
  light: { ...cssVariables(LIGHT, "light"), ...mantineVariables(LIGHT) },
  dark: { ...cssVariables(DARK, "dark"), ...mantineVariables(DARK) },
});
