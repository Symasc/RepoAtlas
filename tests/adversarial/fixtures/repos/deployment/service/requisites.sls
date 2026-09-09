first_state:
  cmd.run:
    - name: /usr/bin/true

second_state:
  cmd.run:
    - name: /usr/bin/true
    - require_in:
      - cmd: third_state
    - watch:
      - cmd: first_state

third_state:
  cmd.run:
    - name: /usr/bin/true
    - onchanges_in:
      - cmd: first_state
