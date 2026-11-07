import { createApp } from './app'

export function startServer(spec: string) {
  return createApp(spec)
}
