import { expressAppConfig } from 'oas3-tools'
import { handlers } from './handlers'
import SPEC from './paths'

const appConfig = expressAppConfig(SPEC, { routing: { controllers: handlers } })
export const app = appConfig.getApp()
