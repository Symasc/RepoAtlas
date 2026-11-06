import path from 'path'
import { expressAppConfig } from 'oas3-tools'
import { handlers } from './handlers'
export function createApp(specPath: string) {
  const appConfig = expressAppConfig(path.join(__dirname, specPath), { routing: { controllers: handlers } })
  return appConfig.getApp()
}
export function boot(spec: string) {
  return createApp(spec)
}
