import { commonHost } from "@company/common";
import { describeRetries } from "./retry";

export interface ServiceSettings {
  host: string;
  retries: number;
}

export function readSettings(): ServiceSettings {
  // GROUND TRUTH: SERVICE_HOST is read here and nowhere else in this repository.
  const host = process.env.SERVICE_HOST ?? commonHost();
  const retries = Number(process.env["CONNECT_RETRIES"] ?? 3);
  describeRetries(retries);
  return { host, retries };
}
