import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

export default defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    // Playwright fixtures call their callback `use`, which is not React's hook
    files: ["e2e/**"],
    rules: { "react-hooks/rules-of-hooks": "off" },
  },
  globalIgnores([
    ".next/**",
    ".next-e2e/**",
    ".next-ui/**",
    "out/**",
    "next-env.d.ts",
    "lib/api/generated.ts",
    "playwright-report/**",
    "test-results/**",
  ]),
]);
