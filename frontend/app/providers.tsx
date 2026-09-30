"use client";

import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import { makeQueryClient } from "@/lib/api/queries";
import { resolver, theme } from "@/lib/theme/theme";

export function Providers({ children }: { children: ReactNode }) {
  // one client per browser session; queries here never suspend, so state is safe
  const [client] = useState(makeQueryClient);
  return (
    <MantineProvider theme={theme} cssVariablesResolver={resolver} defaultColorScheme="auto">
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </MantineProvider>
  );
}
