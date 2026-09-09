include:
  - service.pillar

service_config:
  file.managed:
    - name: /etc/service/service.conf
    - source: salt://service/files/service.conf.jinja
    - template: jinja
    - context:
        SERVICE_HOST: {{ pillar['service']['host'] }}
        SERVICE_PORT: {{ pillar['service']['port'] }}
    - require:
      - pkg: service_package

service_package:
  pkg.installed:
    - name: service
