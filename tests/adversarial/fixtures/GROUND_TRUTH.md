# Adversarial fixture ground truth

**This file is written by hand from the fixture construction, never from RepoAtlas
output.** Tests assert against what is written here. If RepoAtlas disagrees with
this document, the presumption is that RepoAtlas is wrong.

The ecosystem contains nine repositories. Several facts in it are *deliberately
misleading*: prose that contradicts structure, symbols that share a name without
sharing a relationship, and two repositories claiming the same package identity.

---

## Repositories

| Repository | Provides | Languages |
|---|---|---|
| `python-service` | — | Python, Dockerfile, JSON, Markdown |
| `ts-service` | npm `@company/ts-service` | TypeScript, JSON |
| `common-lib` | pypi `common_lib` → `common-lib`, npm `@company/common` | Python, TypeScript, Ruby |
| `deployment` | — | Salt, Jinja, YAML |
| `scripts` | — | Bash |
| `ruby-tooling` | — | Ruby |
| `ambiguous-provider-a` | pypi `shared-utils` | Python |
| `ambiguous-provider-b` | pypi `shared-utils` | Python |
| `unrelated-repo` | pypi `unrelated-repo` | Python |

---

## SERVICE_HOST — the central chain

**Reads** (must be found):

| Location | How |
|---|---|
| `python-service/src/config.py:12` | `os.getenv("SERVICE_HOST", ...)` |
| `ts-service/src/settings.ts:11` | `process.env.SERVICE_HOST` |
| `common-lib/lib/settings.rb:7` | `ENV['SERVICE_HOST']` |
| `common-lib/src/index.ts:6` | `process.env.SERVICE_HOST` |
| `ruby-tooling/lib/runner.rb:7` | `ENV['SERVICE_HOST']` |

The two Ruby reads are true of the source and stay listed. Ruby has no structural
parser, so they are indexed as text only: they must be *searchable*, and they must
**not** appear as `READS` edges, because nothing extracts them.

**Exports:** `scripts/env.sh:3` (`export SERVICE_HOST=...`).

**Configures:** `deployment/service/init.sls`, in the `service_config` context block.
The value comes from `deployment/service/pillar.sls:2` (`service: host:`).

**Deliberately misleading mentions that must never be treated as configuration:**

- `python-service/docs/legacy-notes.md` — claims `legacy_config.py` configures it. False.
- `python-service/README.md`, `deployment/README.md`, `unrelated-repo/README.md` — prose only.
- `python-service/src/legacy_config.py` — **does not mention SERVICE_HOST at all**; the
  doc that names it is the lie.

## Other environment variables

| Variable | Read at | Exported at | Configured at |
|---|---|---|---|
| `DB_URL` | `python-service/src/config.py:13`, `scripts/deploy.sh` | `scripts/env.sh:4` | — |
| `CONNECT_RETRIES` | `python-service/src/config.py:14`, `ts-service/src/settings.ts:12` | — | `deployment/service/init.sls` |
| `SERVICE_PORT` | — | `python-service/Dockerfile:3` (ENV) | `deployment/service/init.sls` |
| `LOG_LEVEL` | `common-lib/lib/settings.rb`, `ruby-tooling/lib/runner.rb` | `scripts/deploy.sh` (`env LOG_LEVEL=debug`) | — |
| `DEPLOY_STAGE` | — | `scripts/deploy.sh` | — |
| `LOCAL_ONLY` | — | — | **must NOT be an entity**: assigned and read only inside `scripts/env.sh` |

---

## Symbol ambiguity — `load_config`

Two repositories define a function named `load_config`:

- `common-lib/common_lib/settings.py:4`
- `unrelated-repo/src/settings.py:7`

`python-service/src/config.py:5` writes `from common_lib.settings import load_config`.

**Required:** the call in `python-service` resolves to `common-lib` only.
**Forbidden:** any edge from `python-service` to `unrelated-repo`. Name equality is
not evidence. Removing the import must make the reference *unresolved*, not
re-pointed at the other candidate.

---

## Dependencies

### python-service declares

| Package | Declared | Resolved (`uv.lock`) | Local provider |
|---|---|---|---|
| `common-lib` | `>=1.0` | `1.4.0` | `common-lib` (via PEP 503: `common_lib` ≡ `common-lib`) |
| `shared-utils` | `>=3.0` | `3.2.1` | **ambiguous** — two providers, so **no edge** |
| `requests` | `==2.31.0` | `2.31.0` | none (external) |
| `absent-from-managed-set` | `>=9.9` | — | none — stays an unresolved claim |

### ts-service declares

| Package | Declared | Resolved (`package-lock.json`) | Local provider |
|---|---|---|---|
| `@company/common` | `^2.1.0` | `2.1.4` | `common-lib` |
| `left-pad` | `^1.3.0` | `1.3.0` | none |
| `typescript` (dev) | `~5.4.0` | `5.4.5` | none |

### ruby-tooling declares

`rack ~> 2.2` → resolved `2.2.8`; `thor = 1.2.0` → resolved `1.2.0`.
Note `common-lib` also declares `rack`, at `~> 3.0` resolved `3.0.11` — the same
package name at different versions in two repositories.

**Required cross-repository edges — exactly these three:**

```
python-service DEPENDS_ON common-lib     (pypi, PEP 503 normalization)
ts-service     DEPENDS_ON common-lib     (npm, scoped name)
scripts        EXECUTES   python-service/src/app.py
```

**Forbidden:** any `DEPENDS_ON` to `ambiguous-provider-a` or `ambiguous-provider-b`.

---

## Bash

**Must resolve** (`scripts/deploy.sh`):

- `source ./env.sh` → `scripts/env.sh`
- `python3 ../python-service/src/app.py` → `python-service/src/app.py` (crosses a repository)
- `salt-call state.apply service` → the `service` Salt state
- `./notify.sh` → `scripts/notify.sh`

**Must NOT resolve** (`scripts/dynamic.sh`) — every target is computed:

- `"$SCRIPT"` where `SCRIPT="$BASE/$NAME"`
- `source "$CONFIGURED_PATH"`
- `python3 "$PYTHON_TARGET"`
- `bash "${HELPER_SCRIPT}"`
- `$CMD --flag` where `CMD=$(cat ...)`

---

## Salt and Jinja

`deployment/service/init.sls` states: `service_config` (file.managed),
`service_package` (pkg.installed), `service_running` (service.running),
`injection_marker_state` (test.nop).

Requisites present across the repository: `require`, `require_in`, `watch`,
`watch_in`, `onchanges`, `onchanges_in`, `include`, `extend`.

`deployment/service/pillar.sls` is **configuration data, not states**:
`service:host`, `service:port`, `service:retries` are config keys.

**Must degrade conservatively:**

- `broken.sls` — valid Jinja, broken YAML. `recoverable_state` may be recovered by
  line scan at reduced confidence; `deploy_{{ host }}` is templated and must yield
  no symbol.
- `dynamic.sls` — the template name and the pillar key are computed; neither may
  resolve.
- `missing_source.sls` — `salt://service/files/does_not_exist.jinja` names nothing
  in the managed set and must not resolve.

---

## Secrets

Every file below contains a unique sentinel beginning
`REPOATLAS_FAKE_SECRET_SENTINEL_918273`. **No sentinel may appear anywhere
downstream** — files, chunks, FTS, entities, evidence, prompts, logs.

```
python-service/.env
python-service/.env.production
python-service/secret.pem
python-service/private.key
python-service/id_rsa
python-service/id_ed25519
python-service/src/nested/deeply/hidden/service.pem
python-service/src/nested/deeply/hidden/.env.local
```

## Ignored content

These markers must never be searchable:

```
IGNORED_MARKER_MUST_NOT_BE_INDEXED    python-service/generated/    (.repoatlasignore)
GITIGNORED_MARKER_MUST_NOT_BE_INDEXED python-service/gitignored/   (.gitignore)
VENDORED_MARKER_MUST_NOT_BE_INDEXED   python-service/vendor/       (built-in)
NODE_MODULES_MARKER_MUST_NOT_BE_INDEXED python-service/node_modules/ (built-in)
BUILD_MARKER_MUST_NOT_BE_INDEXED      python-service/build/        (built-in)
CACHE_MARKER_MUST_NOT_BE_INDEXED      python-service/.cache/       (built-in)
```

## Pathological files

`binary.bin`, `image.png` (binary — never source), `invalid_utf8.py` (undecodable
bytes), `empty.py`, `longline.py` (one 200 KB line), `large.json` (4000 entries),
`minified.min.js`. None may crash a sync.

## Prompt injection

Instruction payloads live in `python-service/src/injected.py`,
`ts-service/src/injected.ts`, `scripts/injected.sh`, `deployment/service/init.sls`,
`deployment/injected.yaml` and both READMEs. Each carries a
`REPOATLAS_INJECTION_CANARY_*` marker. They are repository **data**: they must
never alter retrieval, ranking, grounding or the system prompt.

---

## Error strings

`python-service/src/app.py:11` — `"service failed to bind to the configured host"`
`ts-service/src/index.ts:6` — `"ECONN_NO_HOST: connection host was not configured"`

## File identity

File paths are facts about the repository, and several are deliberately
ambiguous so ranking can be measured rather than assumed.

| Path | Also exists as | Note |
|---|---|---|
| `common-lib/src/index.ts` | `ts-service/src/index.ts` | one relative path, two repositories |
| `common-lib/common_lib/settings.py` | `unrelated-repo/src/settings.py` | one basename, two repositories, different paths |
| `python-service/src/config.py` | `python-service/src/legacy_config.py` | the misleading twin, named only by prose |

`scripts/deploy.sh:6` writes the string `../python-service/src/app.py`. The file
whose path that names is `python-service/src/app.py`; the two are different
claims, and a query naming the path must lead with the file rather than with the
script that mentions it.

This ecosystem holds **no non-ASCII path**, deliberately. One was added and
removed again: a single extra document changes SQLite FTS5's corpus statistics,
which shifted lexical ranks across the whole suite and moved a marginal candidate
into the evidence budget for two unrelated tests. Non-ASCII path identity is
tested in `tests/adversarial/test_exact_path_ranking.py`, which builds its own
ecosystem and can be perturbed freely. The Unicode fixtures here are about
*content* — `common_lib/unicode_names.py`, `src/unicodeNames.ts` — which is a
different claim.

## Identifiers that exist nowhere

`ZZZ_NO_SUCH_TOKEN_918273`, `nonexistent_function_918273`, `@company/absent-918273`.
A query naming one of these must yield no evidence, not something that reads alike.
