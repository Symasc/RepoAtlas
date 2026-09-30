import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  devIndicators: false,
  // `next dev` would otherwise write AGENTS.md and CLAUDE.md here; RepoAtlas keeps
  // agent guidance in one vendor-neutral place (skills/), and nowhere else.
  agentRules: false,
  // The end-to-end run builds into its own directory, so it never disturbs `pnpm dev`.
  distDir: process.env.RA_DIST_DIR ?? ".next",
  experimental: {
    optimizePackageImports: ["@mantine/core", "@mantine/hooks", "@phosphor-icons/react"],
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "no-referrer" },
        ],
      },
    ];
  },
};

export default config;
