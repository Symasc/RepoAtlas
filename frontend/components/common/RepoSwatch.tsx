import classes from "./RepoSwatch.module.css";

/**
 * A repository's identity hue, as a small square beside its name: the one mark
 * a repository has, wherever it appears, and never the only label.
 */
export function RepoSwatch({ hue, size = 10 }: { hue: number | null; size?: number }) {
  return (
    <span
      className={classes.swatch}
      style={{ width: size, height: size, ...(hue ? { backgroundColor: `var(--ra-repo-${hue})` } : {}) }}
      aria-hidden="true"
    />
  );
}
