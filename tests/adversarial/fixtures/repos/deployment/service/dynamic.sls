{% set template_name = grains['os'] ~ '.jinja' %}
{% set pillar_key = 'service:' ~ grains['id'] %}

dynamic_config:
  file.managed:
    - name: /etc/service/dynamic.conf
    - source: salt://service/files/{{ template_name }}
    - context:
        VALUE: {{ salt['pillar.get'](pillar_key) }}
