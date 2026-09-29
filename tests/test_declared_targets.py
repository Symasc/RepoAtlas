"""Every resolver reads a package declaration as the target it names, and reads it here.

:func:`~repoatlas.graph.declared.classify_dependency_target` is the one reading a
workspace member, a package another repository provides and a package-level dependent
all share; these pin what each spelling names.
"""

from __future__ import annotations

from pathlib import PureWindowsPath

import pytest
from repoatlas.graph.declared import (
    TargetKind,
    classify_dependency_target,
    path_target,
    root_path,
)
from repoatlas.graph.packages import ProviderTargets, Provision

WANTED = "@fixture/shared"


@pytest.mark.parametrize(
    ("constraint", "kind", "path"),
    [
        ("", TargetKind.VERSION, None),
        ("^1.2.0", TargetKind.VERSION, None),
        (">=1.0.0 <2", TargetKind.VERSION, None),
        ("latest", TargetKind.VERSION, None),
        ("catalog:backend", TargetKind.VERSION, None),
        ("npm:@fixture/shared@1.0.0", TargetKind.VERSION, None),
        ("npm:@Fixture/Shared", TargetKind.VERSION, None),
        ("npm:shared@1.0.0", TargetKind.ALIAS, None),
        ("npm:@fixture/other@^1", TargetKind.ALIAS, None),
        ("workspace:", TargetKind.WORKSPACE, None),
        ("workspace:*", TargetKind.WORKSPACE, None),
        ("workspace:^", TargetKind.WORKSPACE, None),
        ("workspace:~", TargetKind.WORKSPACE, None),
        ("workspace:^1.2.0", TargetKind.WORKSPACE, None),
        ("workspace:@fixture/shared@*", TargetKind.WORKSPACE, None),
        ("workspace:@fixture/other@*", TargetKind.ALIAS, None),
        ("workspace:other@^1", TargetKind.ALIAS, None),
        ("workspace:../shared", TargetKind.PATH, "../shared"),
        ("workspace:./", TargetKind.PATH, "./"),
        ("file:../shared", TargetKind.PATH, "../shared"),
        ("link:../../repo/shared", TargetKind.PATH, "../../repo/shared"),
        ("portal:../shared", TargetKind.PATH, "../shared"),
        ("file:vendor/shared.tgz", TargetKind.PATH, "vendor/shared.tgz"),
        ("../shared", TargetKind.PATH, "../shared"),
        ("./shared", TargetKind.PATH, "./shared"),
        ("file:/abs/shared", TargetKind.ELSEWHERE, None),
        ("link:~/shared", TargetKind.ELSEWHERE, None),
        ("/abs/shared", TargetKind.ELSEWHERE, None),
        ("~/shared", TargetKind.ELSEWHERE, None),
        ("workspace:/abs/shared", TargetKind.ELSEWHERE, None),
        ("owner/shared", TargetKind.ELSEWHERE, None),
        ("github:owner/shared", TargetKind.ELSEWHERE, None),
        ("git+https://example.invalid/shared.git", TargetKind.ELSEWHERE, None),
        ("https://example.invalid/shared.tgz", TargetKind.ELSEWHERE, None),
        ("jsr:@fixture/shared@1", TargetKind.ELSEWHERE, None),
        ("workspace:some/form", TargetKind.UNREADABLE, None),
        ("workspace:@fixture/shared", TargetKind.UNREADABLE, None),
    ],
)
def test_each_spelling_names_one_kind_of_target(
    constraint: str, kind: TargetKind, path: str | None
) -> None:
    target = classify_dependency_target(constraint, WANTED)
    assert (target.kind, target.path) == (kind, path)


@pytest.mark.parametrize(
    ("manifest_dir", "path", "expected"),
    [
        ("", "../provider", "/work/provider"),
        ("apps/app", "../../../provider/", "/work/provider"),
        ("apps/app", "../lib", "/work/consumer/apps/lib"),
        ("", ".", "/work/consumer"),
        ("", "", "/work/consumer"),
        ("pkg", "./a/../b", "/work/consumer/pkg/b"),
    ],
)
def test_a_path_is_followed_from_the_manifest_without_reading_the_disk(
    manifest_dir: str, path: str, expected: str
) -> None:
    assert path_target("/work/consumer", manifest_dir, path) == expected


def test_a_windows_root_is_followed_as_a_directory_not_as_text() -> None:
    consumer = root_path(PureWindowsPath(r"C:\ws\consumer"))
    provider = root_path(PureWindowsPath(r"C:\ws\provider"))
    assert consumer == "C:/ws/consumer"
    assert path_target(consumer, "", "../provider") == "C:/ws/provider"
    targets = ProviderTargets(
        {1: consumer, 2: provider}, [Provision(2, "", "package.json", 2, "p", "p")]
    )
    found = targets.by_path(1, "package.json", "../provider", "p")
    assert not isinstance(found, str)
    assert (found[0].repo_id, found[1]) == (2, {"resolved_by": "path"})
