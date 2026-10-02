# Releasing

RepoAtlas is published to PyPI as `symasc-repoatlas` (`repoatlas` on PyPI is another project). The import package
and the command stay `repoatlas`.

A release is a version tag. Pushing `v0.1.0` runs [`.github/workflows/release.yml`](../.github/workflows/release.yml):

1. **test**: ruff, pyright and the test suite on the tagged commit, then the explorer's checks and unit tests
   (`pnpm check` in `frontend/`).
2. **build**: the tag must name the version in `pyproject.toml` and `repoatlas.__version__`, or the run stops. Then
   it exports the explorer (`pnpm build:static`) into the package at `repoatlas/ui/explorer/`, builds the wheel
   and the source distribution, and smoke-tests each one as installed
   ([`tests/packaging/release_smoke.py`](../tests/packaging/release_smoke.py)): version, migrations, skills,
   `init`, `locate`, an MCP handshake, and `repoatlas ui` serving the explorer and answering its pages while
   refusing a cross-site call.
3. **publish**: uploads both files to PyPI with attestations, by Trusted Publishing. No token is stored anywhere.
4. **release**: creates the GitHub release for the tag, with the files attached and generated notes. Tags such as
   `v0.2.0rc1` are marked as pre-releases.

If any step fails, nothing after it runs: a tag whose tests fail publishes nothing.

## One-time setup

**On PyPI** (Account settings → Publishing → Add a new pending publisher, GitHub tab):

| Field | Value |
|---|---|
| PyPI project name | `symasc-repoatlas` |
| Owner | `Symasc` |
| Repository name | `RepoAtlas` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

A pending publisher lets the first release create the project. After it, the publisher is an ordinary trusted
publisher of that project.

**On GitHub** (Settings → Environments → New environment): create `pypi`. To approve each upload by hand, add
yourself as a required reviewer there; the run then waits at **publish** until you approve.

## Each release

```bash
# 1. set the version in both places (they must agree with the tag)
#    pyproject.toml          version = "0.2.0"
#    repoatlas/__init__.py   __version__ = "0.2.0"
# 2. commit and push it, then tag that commit and push the tag
git tag -a v0.2.0 -m v0.2.0
git push origin v0.2.0
```

## Building locally

`uv build` alone makes a RepoAtlas without the explorer, because the export is build output that git ignores. To
build what a release builds:

```bash
(cd frontend && pnpm install --frozen-lockfile && pnpm build:static)
rm -rf repoatlas/ui/explorer && cp -r frontend/out repoatlas/ui/explorer
uv build
uv run --isolated --no-project --with dist/symasc_repoatlas-0.1.0-py3-none-any.whl \
    tests/packaging/release_smoke.py 0.1.0
rm -rf repoatlas/ui/explorer   # optional: a checkout's `repoatlas ui` uses frontend/ regardless
```

A version on PyPI can never be uploaded again, even after deleting it. If a release goes wrong after **publish**,
fix it and release the next version.
