/**
 * The mark: a folded map whose panels are repositories, crossed by one link.
 * Drawn in the current text colour, with the link in the accent.
 */
export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M3 6.2 9 4l6 2.2L21 4v13.8L15 20l-6-2.2L3 20z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path d="M9 4v13.8M15 6.2V20" stroke="currentColor" strokeWidth="1.5" opacity="0.45" />
      <path
        d="M6.2 14.2 17.8 9.6"
        stroke="var(--ra-accent)"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
      <circle cx="6.2" cy="14.2" r="1.9" fill="var(--ra-accent)" />
      <circle cx="17.8" cy="9.6" r="1.9" fill="var(--ra-accent)" />
    </svg>
  );
}
