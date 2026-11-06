import { expressAppConfig } from 'oas3-tools'
import { widgetHandlers } from './handlers/widgets'

const appConfig = expressAppConfig('api/openapi.yaml', { routing: { controllers: widgetHandlers } })
export const app = appConfig.getApp()
