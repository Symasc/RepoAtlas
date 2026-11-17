"""A repository's remotes, read from its Git configuration on disk.

Git is not asked: ``git config`` is outside the hardened client's allowlist
(:mod:`repoatlas.gitstate.client`), and reading the file answers the question without
running anything. The configuration is found as git finds it: ``.git/config``, or for
a worktree or a submodule checkout, whose ``.git`` is a file, the directory its
``gitdir:`` names -- and that directory's ``commondir`` when it has one.

Only identities are returned (:func:`~repoatlas.gitstate.config.normalize_git_url`),
never a URL as written, so a credential embedded in a remote cannot be stored. A
``url.<base>.insteadOf`` rewrite the same file states is applied first, as git applies
it; an ``include`` is not followed.
"""

from __future__ import annotations

from pathlib import Path

from repoatlas.gitstate.config import normalize_git_url, parse_git_config

#: The most bytes of one configuration or pointer file read.
MAX_CONFIG_BYTES = 1 << 20


def _read(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_CONFIG_BYTES:
        return None
    return raw.decode("utf-8", errors="replace")


def git_directory(repo_path: Path) -> Path | None:
    """The repository's git directory: ``.git``, or what a ``.git`` file points at."""
    marker = repo_path / ".git"
    if marker.is_dir():
        return marker
    if not marker.is_file():
        return None
    text = _read(marker)
    if text is None or not text.startswith("gitdir:"):
        return None
    target = Path(text[len("gitdir:") :].strip())
    return target if target.is_absolute() else (repo_path / target)


def config_file(git_dir: Path) -> Path:
    """Where the configuration of ``git_dir`` is: its common directory's, if it shares one."""
    common = git_dir / "commondir"
    if common.is_file():
        text = _read(common)
        if text and text.strip():
            shared = Path(text.strip())
            return (shared if shared.is_absolute() else git_dir / shared) / "config"
    return git_dir / "config"


def read_remotes(repo_path: Path) -> tuple[str, ...]:
    """The identities of the repository's remotes: ``origin`` first, then by name.

    Empty when the repository has none, or its configuration cannot be read.
    """
    git_dir = git_directory(repo_path)
    if git_dir is None:
        return ()
    text = _read(config_file(git_dir))
    if text is None:
        return ()
    entries = parse_git_config(text)
    rewrites = sorted(
        (
            (entry.value, entry.subsection)
            for entry in entries
            if entry.section == "url"
            and entry.key == "insteadof"
            and entry.value
            and entry.subsection
        ),
        key=lambda pair: -len(pair[0]),
    )
    urls: dict[str, str] = {}
    for entry in entries:
        if entry.section != "remote" or entry.key != "url" or not entry.value:
            continue
        if entry.subsection is None or entry.subsection in urls:
            continue
        url = entry.value
        for prefix, replacement in rewrites:
            if url.startswith(prefix):
                url = replacement + url[len(prefix) :]
                break
        urls[entry.subsection] = url
    found: list[str] = []
    for name in sorted(urls, key=lambda n: (n != "origin", n)):
        identity = normalize_git_url(urls[name], base_dir=str(repo_path))
        if identity is not None and identity not in found:
            found.append(identity)
    return tuple(found)
