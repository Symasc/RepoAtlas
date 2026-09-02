import { readSettings } from "./settings";

export function start(): void {
  const settings = readSettings();
  if (!settings.host) {
    throw new Error("ECONN_NO_HOST: connection host was not configured");
  }
}
