const path = require('path')
const { expressAppConfig } = require('oas3-tools')

const handlers = {
  getStatus(req, res) { res.end() },
  listWidgets(req, res) { res.end() },
}

function createApp(specPath) {
  const appConfig = expressAppConfig(path.join(__dirname, specPath), { routing: { controllers: handlers } })
  return appConfig.getApp()
}

module.exports = { createApp }
