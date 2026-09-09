import { getConfig } from "@company/common";
import { servicePort } from "./settings";

export function start(): string {
  const host = process.env.SERVICE_HOST ?? "localhost";
  return `${getConfig()}:${host}:${servicePort()}`;
}
