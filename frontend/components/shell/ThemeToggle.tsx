"use client";

import {
  ActionIcon,
  Box,
  Tooltip,
  useComputedColorScheme,
  useMantineColorScheme,
} from "@mantine/core";
import { useMounted } from "@mantine/hooks";
import { MoonIcon, SunIcon } from "@phosphor-icons/react";

import classes from "./ThemeToggle.module.css";

/**
 * The server cannot know a scheme chosen in this browser, so nothing it renders
 * may depend on one: both icons are rendered and CSS shows the right one (the
 * scheme attribute is set before first paint), and the label names the target
 * scheme only once mounted. The icons take no inline `display`, which would
 * override the class that hides them.
 */
export function ThemeToggle() {
  const { setColorScheme } = useMantineColorScheme();
  const scheme = useComputedColorScheme("dark");
  const mounted = useMounted();
  const next = scheme === "dark" ? "light" : "dark";
  const label = mounted ? `Switch to ${next} theme` : "Switch theme";
  return (
    <Tooltip label={label}>
      <ActionIcon onClick={() => setColorScheme(next)} aria-label={label}>
        <Box component="span" lightHidden className={classes.glyph}>
          <SunIcon size={18} />
        </Box>
        <Box component="span" darkHidden className={classes.glyph}>
          <MoonIcon size={18} />
        </Box>
      </ActionIcon>
    </Tooltip>
  );
}
