Symptom under investigation:

$question

$scope

Evidence retrieved from the repositories:

$evidence

Diagnose this from the evidence.

- `summary`: the most likely explanation in one or two sentences, or a statement
  that the evidence is insufficient to explain it.
- `findings`: candidate causes, most likely first. Each needs citations and a
  `confidence` reflecting how strongly the evidence supports it. State a cause as
  a possibility unless the evidence proves it.
- `unknowns`: what you would need to see to confirm or rule out a cause —
  runtime values, logs, configuration, or code not present in the evidence.
- `next_check`: the single most informative diagnostic step to take next.

Do not assert a root cause the evidence only makes plausible. Ranking likely
causes is useful; declaring an unproven one is not.
