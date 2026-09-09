"""Retrieval evaluation runner (V1_SPEC §14, §19).

Loads the hand-written cases in ``retrieval_cases.yaml``, runs each through the
retrieval funnel, and reports recall, forbidden-evidence and unknown-rejection
metrics **per category**.

Deliberately never reports one combined "accuracy". Exact lookup, prose questions
and unanswerable queries fail in different ways and for different reasons; one
number hides which of them regressed, and a suite nobody can act on is a suite
nobody reads.

Retrieval is evaluated on its own, without a reasoning model: what the funnel
puts in front of the LLM is the thing that determines whether an answer can be
grounded at all.
"""

from __future__ import annotations

import argparse
import dataclasses
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from repoatlas.models.evidence import EvidenceSet, EvidenceTier, MatchClass
from repoatlas.services.query_service import QueryIntent, locate, retrieve_for
from repoatlas.services.workspace import Workspace

CASES_PATH = Path(__file__).parent / "retrieval_cases.yaml"

#: Rank windows reported for every category.
K_VALUES = (1, 3, 5, 10)


@dataclasses.dataclass(frozen=True)
class Case:
    id: str
    category: str
    command: str
    query: str
    context_repo: str
    required_evidence: tuple[str, ...] = ()
    alternate_evidence: tuple[str, ...] = ()
    forbidden_evidence: tuple[str, ...] = ()
    expect_unknown: bool = False
    expect_cross_repo: bool = False
    #: The :class:`MatchClass` every ``required_evidence`` path must come back
    #: with, by name (``exact_path``, ``exact_basename``, …). Written for cases
    #: whose point is *how* the file was identified rather than only whether it
    #: was found: a query naming a whole path that comes back as a basename match
    #: has been answered by coincidence, and recall alone cannot see the
    #: difference.
    expect_match: str = ""
    recall_k: int = 5
    notes: str = ""

    @property
    def is_global(self) -> bool:
        return self.context_repo == "__root__"

    @property
    def required_match(self) -> MatchClass | None:
        return MatchClass[self.expect_match.upper()] if self.expect_match else None


@dataclasses.dataclass
class Outcome:
    case: Case
    returned: tuple[str, ...]
    #: Rank (1-based) at which each required path first appears, or None.
    ranks: dict[str, int | None]
    forbidden_hits: tuple[str, ...]
    repositories: frozenset[str]
    deterministic_before_associative: bool
    evidence: EvidenceSet
    #: Strongest match class returned for each required path, or ``None`` if the
    #: path did not come back at all.
    matches: dict[str, MatchClass | None] = dataclasses.field(default_factory=dict)

    @property
    def match_correct(self) -> bool | None:
        """Whether every required path was identified the way the case expects."""
        expected = self.case.required_match
        if expected is None:
            return None
        return all(actual is expected for actual in self.matches.values())

    def recall_at(self, k: int) -> bool | None:
        """Whether every required path appears within the first ``k``.

        ``None`` for cases with no required evidence, which are scored by the
        unknown-rejection metric instead and must not dilute recall.
        """
        if not self.case.required_evidence:
            return None
        return all(rank is not None and rank <= k for rank in self.ranks.values())

    @property
    def unknown_correct(self) -> bool | None:
        if not self.case.expect_unknown:
            return None
        return not self.returned

    @property
    def cross_repo_correct(self) -> bool | None:
        if not self.case.expect_cross_repo:
            return None
        return len(self.repositories) > 1


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases: list[Case] = []
    for entry in raw["cases"]:
        payload = dict(entry)
        for key in ("required_evidence", "alternate_evidence", "forbidden_evidence"):
            payload[key] = tuple(payload.get(key) or ())
        cases.append(Case(**payload))
    return cases


def _retrieve(workspace: Workspace, case: Case, context) -> EvidenceSet:
    """Retrieve exactly the way the named command would.

    ``locate`` has no intent of its own — it renders the evidence set directly —
    while ``debug`` widens the funnel and searches the whole question literally so
    an unquoted error string matches. Evaluating through the same entry points the
    CLI uses keeps the metric about the product rather than about a helper.
    """
    if case.command == "locate":
        return locate(workspace, case.query, context)
    intent = QueryIntent.DEBUG if case.command == "debug" else QueryIntent.ASK
    return retrieve_for(workspace, case.query, context, intent)


def run_case(workspace: Workspace, root: Path, case: Case) -> Outcome:
    cwd = root if case.is_global else root / case.context_repo
    context = workspace.resolver().resolve(cwd)
    evidence = _retrieve(workspace, case, context)

    returned = tuple(f"{c.repository}/{c.path}" for c in evidence.candidates)
    ranks = {
        required: next((i for i, path in enumerate(returned, 1) if path == required), None)
        for required in case.required_evidence
    }
    tiers = [c.tier for c in evidence.candidates]
    matches: dict[str, MatchClass | None] = {}
    for required in case.required_evidence:
        claimed = [c.match for c in evidence.candidates if f"{c.repository}/{c.path}" == required]
        matches[required] = min(claimed) if claimed else None
    return Outcome(
        case=case,
        returned=returned,
        ranks=ranks,
        forbidden_hits=tuple(p for p in returned if p in case.forbidden_evidence),
        repositories=frozenset(c.repository for c in evidence.candidates),
        deterministic_before_associative=tiers == sorted(tiers),
        evidence=evidence,
        matches=matches,
    )


def run_all(workspace: Workspace, root: Path, cases: Sequence[Case]) -> list[Outcome]:
    return [run_case(workspace, root, case) for case in cases]


# -- metrics -----------------------------------------------------------------


def _fraction(values: Sequence[bool]) -> str:
    if not values:
        return "   n/a"
    return f"{sum(values)}/{len(values)}"


def report(outcomes: Sequence[Outcome]) -> str:
    """A per-category table. No combined figure, by design."""
    by_category: dict[str, list[Outcome]] = defaultdict(list)
    for outcome in outcomes:
        by_category[outcome.case.category].append(outcome)

    header = (
        f"{'category':<14}{'n':>4}"
        + "".join(f"{'r@' + str(k):>8}" for k in K_VALUES)
        + f"{'forbid':>8}{'unknown':>9}{'x-repo':>8}{'tier':>7}{'match':>8}"
    )
    lines = [header, "-" * len(header)]

    for category in sorted(by_category):
        group = by_category[category]
        row = f"{category:<14}{len(group):>4}"
        for k in K_VALUES:
            scored = [r for r in (o.recall_at(k) for o in group) if r is not None]
            row += f"{_fraction(scored):>8}"
        row += f"{sum(1 for o in group if o.forbidden_hits):>8}"
        unknown = [u for u in (o.unknown_correct for o in group) if u is not None]
        row += f"{_fraction(unknown):>9}"
        cross = [c for c in (o.cross_repo_correct for o in group) if c is not None]
        row += f"{_fraction(cross):>8}"
        row += f"{sum(1 for o in group if not o.deterministic_before_associative):>7}"
        graded = [m for m in (o.match_correct for o in group) if m is not None]
        row += f"{_fraction(graded):>8}"
        lines.append(row)

    lines.append("")
    lines.append(
        "forbid = cases returning forbidden evidence · "
        "tier = deterministic-precedence violations · both must be 0 · "
        "match = required evidence returned with the expected match specificity"
    )
    return "\n".join(lines)


def failures(outcomes: Sequence[Outcome]) -> list[str]:
    """Every case that did not meet its own contract, with the reason."""
    problems: list[str] = []
    for outcome in outcomes:
        case = outcome.case
        if outcome.forbidden_hits:
            problems.append(f"{case.id}: returned forbidden evidence {outcome.forbidden_hits}")
        if case.expect_unknown and outcome.returned:
            problems.append(f"{case.id}: expected no evidence, got {outcome.returned[:3]}")
        if not outcome.deterministic_before_associative:
            problems.append(f"{case.id}: associative evidence ranked above deterministic")
        if case.expect_cross_repo and len(outcome.repositories) <= 1:
            problems.append(
                f"{case.id}: expected cross-repository evidence, got {sorted(outcome.repositories)}"
            )
        if outcome.match_correct is False:
            wrong = {
                path: (match.name.lower() if match else None)
                for path, match in outcome.matches.items()
                if match is not case.required_match
            }
            problems.append(f"{case.id}: expected match {case.expect_match}, got {wrong}")
        missed = {
            path: rank
            for path, rank in outcome.ranks.items()
            if rank is None or rank > case.recall_k
        }
        if missed:
            problems.append(
                f"{case.id}: required evidence outside recall@{case.recall_k}: {missed}"
            )
    return problems


def _detail(outcome: Outcome) -> str:
    lines = [f"  {outcome.case.id} [{outcome.case.category}] {outcome.case.query!r}"]
    for index, path in enumerate(outcome.returned, 1):
        candidate = outcome.evidence.candidates[index - 1]
        mark = "*" if path in outcome.case.required_evidence else " "
        tier = "D" if candidate.tier is EvidenceTier.DETERMINISTIC else "A"
        lines.append(
            f"   {mark}{index:>3}. [{tier}] {candidate.match.name.lower():<14} "
            f"{path}:{candidate.line_start}"
        )
    if not outcome.returned:
        lines.append("       (no evidence)")
    return "\n".join(lines)


def main() -> int:  # pragma: no cover - operator entry point
    parser = argparse.ArgumentParser(description="Run the RepoAtlas retrieval evaluation.")
    parser.add_argument("--detail", action="store_true", help="print every returned ranking")
    parser.add_argument("--category", help="restrict to one category")
    parser.add_argument(
        "--provider",
        default="hashing",
        choices=("hashing", "fastembed"),
        help="embedding backend; 'fastembed' downloads and runs the real model",
    )
    arguments = parser.parse_args()

    from tests.evaluation.harness import evaluation_workspace

    cases = load_cases()
    if arguments.category:
        cases = [case for case in cases if case.category == arguments.category]

    with evaluation_workspace(arguments.provider) as (workspace, root):
        outcomes = run_all(workspace, root, cases)

    print(report(outcomes))
    if arguments.detail:
        print()
        for outcome in outcomes:
            print(_detail(outcome))
    problems = failures(outcomes)
    if problems:
        print(f"\n{len(problems)} unmet expectation(s):")
        for problem in problems:
            print(f"  - {problem}")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
