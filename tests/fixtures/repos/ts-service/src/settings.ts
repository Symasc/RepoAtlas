export const DEFAULT_PORT = 8080;

export function servicePort(): number {
  return Number(process.env.SERVICE_PORT ?? DEFAULT_PORT);
}
