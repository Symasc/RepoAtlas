# Shared Ruby helper for the deployment tooling.
require "json"
require_relative "logging"

module Common
  class Settings
    def endpoint
      ENV["SERVICE_HOST"]
    end

    def timeout
      ENV.fetch("SERVICE_TIMEOUT", "30")
    end
  end
end
