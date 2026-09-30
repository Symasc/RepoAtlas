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

/**
 * The server cannot know a scheme chosen in this browser, so nothing it renders
 * may depend on one: both icons are rendered and CSS shows the right one (the
 * scheme attribute is set before first paint), and the label names the target
 * scheme only once mounted.
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
        <Box component="span" display="flex" lightHidden>
          <SunIcon size={18} />
        </Box>
        <Box component="span" display="flex" darkHidden>
          <MoonIcon size={18} />
        </Box>
      </ActionIcon>
    </Tooltip>
  );
}
