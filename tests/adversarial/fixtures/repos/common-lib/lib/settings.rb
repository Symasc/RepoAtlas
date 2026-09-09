require 'json'
require_relative 'logging'

module CommonLib
  class Settings
    def self.load
      { host: ENV['SERVICE_HOST'], level: ENV.fetch('LOG_LEVEL', 'info') }
    end
  end
end
