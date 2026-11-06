import path from 'path'
import { expressAppConfig } from 'oas3-tools'
import { handlers } from './handlers'

const appConfig = expressAppConfig(path.resolve(process.cwd(), 'api/openapi.yaml'), { routing: { controllers: handlers } })
export const app = appConfig.getApp()
