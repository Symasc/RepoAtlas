export function getConfig(): string {
  return process.env.COMMON_ENDPOINT ?? "unset";
}
