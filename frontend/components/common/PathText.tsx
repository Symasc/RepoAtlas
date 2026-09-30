import { Fragment } from "react";

/** A repository path that wraps only after a `/`, never inside a name. */
export function PathText({ path }: { path: string }) {
  const parts = path.split("/");
  return (
    <>
      {parts.map((part, index) => (
        <Fragment key={index}>
          {part}
          {index < parts.length - 1 && (
            <>
              /<wbr />
            </>
          )}
        </Fragment>
      ))}
    </>
  );
}
