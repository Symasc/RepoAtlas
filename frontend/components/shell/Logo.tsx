import Image from "next/image";

import mark from "@/assets/brand/repoatlas-mark-256.png";

/** The RepoAtlas mark, from assets/brand (the full-size original lives beside it). */
export function Logo({ size = 24 }: { size?: number }) {
  return <Image src={mark} alt="" width={size} height={size} priority />;
}
