import path from 'path'
import { expressAppConfig } from 'oas3-tools'
import { handlers } from './handlers'

let SPEC = path.join(__dirname, '../api/openapi.yaml')

const appConfig = expressAppConfig(SPEC, { routing: { controllers: handlers } })
export const app = appConfig.getApp()
