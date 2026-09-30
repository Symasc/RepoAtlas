import { Fragment } from "react";

import classes from "./Inline.module.css";

/** Text with `backticked` commands set as code, as the copy in lib/model writes them. */
export function Inline({ text }: { text: string }) {
  return (
    <>
      {text.split("`").map((part, index) =>
        index % 2 === 1 ? (
          <code key={index} className={classes.code}>
            {part}
          </code>
        ) : (
          <Fragment key={index}>{part}</Fragment>
        ),
      )}
    </>
  );
}
