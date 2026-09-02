import { getConfig } from "@company/common";

export function start(): string {
  const host = process.env.SERVICE_HOST ?? "localhost";
  return `${getConfig()}:${host}`;
}
