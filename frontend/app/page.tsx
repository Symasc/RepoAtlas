import { Suspense } from "react";

import { Explorer } from "@/components/explorer/Explorer";

export default function Page() {
  // Explorer reads its state from the URL, which needs a Suspense boundary
  return (
    <Suspense>
      <Explorer />
    </Suspense>
  );
}
