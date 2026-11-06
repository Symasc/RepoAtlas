import path from 'path'
import { expressAppConfig } from 'oas3-tools'
import { handlers } from './handlers'

export function createApp(dir: string) {
  const appConfig = expressAppConfig(path.join(__dirname, dir, 'openapi.yaml'), { routing: { controllers: handlers } })
  return appConfig.getApp()
}

export const app = createApp('../api')
