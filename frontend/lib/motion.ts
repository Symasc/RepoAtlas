/**
 * Whether the explorer animates: the reader's own choice, remembered in this
 * browser, else what the system asks for. MOTION_SCRIPT applies it before first
 * paint as `data-motion` on the root, where one CSS rule stops every transition;
 * the views read the same choice through useAnimationsOff (lib/useMotion.ts)
 * for the movements they make themselves: the camera, a tween.
 */

export const MOTION_KEY = "ra-motion";
export const REDUCE_QUERY = "(prefers-reduced-motion: reduce)";

export type MotionChoice = "on" | "off";

/** The stored choice wins; with none, the system's preference decides. */
export function animationsOff(choice: string | null, systemReduces: boolean): boolean {
  if (choice === "off") return true;
  if (choice === "on") return false;
  return systemReduces;
}

/** Runs in the document head before anything paints: `animationsOff`, inlined. */
export const MOTION_SCRIPT = `try{var c=localStorage.getItem(${JSON.stringify(MOTION_KEY)});var off=c==="off"||(c!=="on"&&matchMedia(${JSON.stringify(REDUCE_QUERY)}).matches);document.documentElement.setAttribute("data-motion",off?"reduced":"full")}catch(e){}`;
