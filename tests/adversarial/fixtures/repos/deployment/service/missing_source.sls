missing_source_state:
  file.managed:
    - name: /etc/service/absent.conf
    - source: salt://service/files/does_not_exist.jinja
