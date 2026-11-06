import path from 'path'
import { expressAppConfig } from 'oas3-tools'
import { handlers } from './handlers'

function specName() { return '../api/openapi.yaml' }

const appConfig = expressAppConfig(path.join(__dirname, specName()), { routing: { controllers: handlers } })
export const app = appConfig.getApp()
