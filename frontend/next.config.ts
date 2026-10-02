import type { NextConfig } from "next";

/** The headers every response carries; the local API sends the same ones for an exported build. */
const SECURITY_HEADERS = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "no-referrer" },
];

/**
 * `pnpm build:static` (RA_EXPORT=1): the explorer as plain files in `out/`, which an
 * installed RepoAtlas serves itself (`repoatlas ui` with no Node). Its one server
 * route, `/api/ra`, is then the local API's to answer, so the build leaves it out:
 * it is the app's only route file with a `.ts` extension.
 */
const exporting = process.env.RA_EXPORT === "1";

const config: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  devIndicators: false,
  // `next dev` would otherwise write AGENTS.md and CLAUDE.md here; RepoAtlas keeps
  // agent guidance in one vendor-neutral place (skills/), and nowhere else.
  agentRules: false,
  experimental: {
    optimizePackageImports: ["@mantine/core", "@mantine/hooks", "@phosphor-icons/react"],
  },
  ...(exporting
    ? { output: "export", distDir: "out", pageExtensions: ["tsx"] }
    : {
        // The end-to-end run builds into its own directory, so it never disturbs `pnpm dev`.
        distDir: process.env.RA_DIST_DIR ?? ".next",
        async headers() {
          return [{ source: "/:path*", headers: SECURITY_HEADERS }];
        },
      }),
};

export default config;
