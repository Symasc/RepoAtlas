"use client";

import { ActionIcon, Tooltip } from "@mantine/core";
import { PauseCircleIcon, PlayCircleIcon } from "@phosphor-icons/react";

import { chooseAnimations, useAnimationsOff } from "@/lib/useMotion";

/**
 * Turns the explorer's animations off or back on, and remembers the choice in
 * this browser. Until one is made, the system's reduced-motion setting decides.
 * The server renders the "on" state; the stored choice replaces it on hydration.
 */
export function MotionToggle() {
  const off = useAnimationsOff();
  const label = off ? "Turn animations on" : "Turn animations off";
  return (
    <Tooltip label={label}>
      <ActionIcon onClick={() => chooseAnimations(off ? "on" : "off")} aria-label={label}>
        {off ? <PlayCircleIcon size={18} /> : <PauseCircleIcon size={18} />}
      </ActionIcon>
    </Tooltip>
  );
}
