export interface CommonOptions {
  host: string;
}

export function commonHost(): string {
  return process.env.SERVICE_HOST ?? "localhost";
}
