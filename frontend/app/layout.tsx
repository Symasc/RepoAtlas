import "@mantine/core/styles.css";
import "@mantine/spotlight/styles.css";
import "./globals.css";

import { ColorSchemeScript, mantineHtmlProps } from "@mantine/core";
import { GeistMono } from "geist/font/mono";
import { GeistSans } from "geist/font/sans";
import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import mark from "@/assets/brand/repoatlas-mark-256.png";

import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "RepoAtlas",
  description: "A map of the repositories RepoAtlas has indexed on this machine.",
  icons: { icon: mark.src },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#0c1420" },
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html
      lang="en"
      {...mantineHtmlProps}
      className={`${GeistSans.variable} ${GeistMono.variable}`}
    >
      <head>
        <ColorSchemeScript defaultColorScheme="auto" />
      </head>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
