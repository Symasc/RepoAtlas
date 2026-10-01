import "@mantine/core/styles.css";
import "@mantine/spotlight/styles.css";
// IBM Plex, served from this app: nothing is fetched from a font host
import "@fontsource-variable/ibm-plex-sans/wght.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/400-italic.css";
import "@fontsource/ibm-plex-mono/500.css";
import "@fontsource/ibm-plex-mono/600.css";
import "./globals.css";

import { ColorSchemeScript, mantineHtmlProps } from "@mantine/core";
import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { MOTION_SCRIPT } from "@/lib/motion";

import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "RepoAtlas",
  description: "A map of the repositories RepoAtlas has indexed on this machine.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#1a1a19" },
    { media: "(prefers-color-scheme: light)", color: "#ece9e3" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" {...mantineHtmlProps}>
      <head>
        <ColorSchemeScript defaultColorScheme="auto" />
        {/* the reader's motion choice, applied before anything can move */}
        <script dangerouslySetInnerHTML={{ __html: MOTION_SCRIPT }} />
      </head>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
