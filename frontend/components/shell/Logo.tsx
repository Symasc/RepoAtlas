import { useId } from "react";

/** The pin the mark's A is drawn under; cut out of the legs with a margin, then drawn. */
const PIN = "M16 17.6 C13.2 14.4 10.4 11.8 10.4 8.6 A5.6 5.6 0 0 1 21.6 8.6 C21.6 11.8 18.8 14.4 16 17.6 Z";

/**
 * The RepoAtlas mark, an A under a map pin, drawn flat in one colour (the
 * current text colour). The original artwork is kept in assets/brand.
 */
export function Logo({ size = 22 }: { size?: number }) {
  const mask = useId();
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden focusable="false">
      <mask id={mask} maskUnits="userSpaceOnUse" x="0" y="0" width="32" height="32">
        <rect width="32" height="32" fill="#fff" />
        <path d={PIN} fill="#000" stroke="#000" strokeWidth="2.8" strokeLinejoin="round" />
      </mask>
      <g mask={`url(#${mask})`} stroke="currentColor" strokeWidth="7.6" strokeLinecap="round">
        <path d="M16 9 L7 26" />
        <path d="M16 9 L25 26" />
      </g>
      <path d={PIN} fill="currentColor" />
    </svg>
  );
}
