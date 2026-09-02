{% for host in pillar['hosts'] %}
deploy_{{ host }}:
  file.managed:
	- bad: [unclosed
{% endfor %}
recoverable_state:
  service.running:
    - require:
      - pkg: recoverable_package
