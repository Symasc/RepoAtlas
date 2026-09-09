module Common
  module Logging
    def self.emit(message)
      warn(message)
    end
  end
end
