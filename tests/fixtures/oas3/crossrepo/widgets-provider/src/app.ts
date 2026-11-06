import path from 'path'
import { expressAppConfig } from 'oas3-tools'
import { widgetHandlers } from './handlers/widgets'

const appConfig = expressAppConfig(path.join(__dirname, '../api/openapi.yaml'), { routing: { controllers: widgetHandlers } })
export const app = appConfig.getApp()
