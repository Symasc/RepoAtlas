require 'json'
require_relative 'support'

module RubyTooling
  class Runner
    def initialize
      @host = ENV['SERVICE_HOST']
      @level = ENV.fetch('LOG_LEVEL', 'warn')
    end

    def run
      Support.prepare(@host)
    end
  end
end
