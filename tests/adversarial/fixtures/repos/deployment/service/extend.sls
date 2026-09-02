include:
  - service

extend:
  service_config:
    file.managed:
      - mode: '0640'
