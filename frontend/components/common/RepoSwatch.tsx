import classes from "./RepoSwatch.module.css";

/** A repository's identity hue, as a small square beside its name; never the only label. */
export function RepoSwatch({ hue, size = 10 }: { hue: number; size?: number }) {
  return (
    <span
      className={classes.swatch}
      style={{ width: size, height: size, backgroundColor: `var(--ra-repo-${hue})` }}
      aria-hidden="true"
    />
  );
}
