import path from 'path'
import { expressAppConfig } from 'oas3-tools'
import { handlers } from './handlers'

const appConfig = expressAppConfig(path.join(__dirname, process.env.NODE_ENV === 'production' ? '../api/openapi.yaml' : '../api/dev.yaml'), { routing: { controllers: handlers } })
export const app = appConfig.getApp()
