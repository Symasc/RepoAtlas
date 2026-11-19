/**
 * Whether the canvas glides to a new fit. Pure, so the rule is tested without a
 * browser: re-centring always glides; a resize or growth glides unless it is held
 * (the overview holds them while the reader is looking at something); and a view
 * the reader has moved is left where it is when only growth asks for a refit.
 */

export interface RefitCause {
  /** The layout re-centred within the level. */
  recentred: boolean;
  /** The canvas changed size. */
  resized: boolean;
  /** The level's extent changed. */
  grown: boolean;
  /** The reader has moved the view since it was last fitted. */
  moved: boolean;
}

export function refitAction(
  cause: RefitCause,
  hold: { growth?: boolean; resize?: boolean } = {},
): "none" | "glide" {
  const resized = cause.resized && !hold.resize;
  const grown = cause.grown && !hold.growth;
  if (!cause.recentred && !resized && !grown) return "none";
  if (!cause.recentred && !resized && cause.moved) return "none";
  return "glide";
}
