"""Unicode identifiers and error strings (V1_SPEC §14).

Exact retrieval must not depend on the alphabet a name is written in. These are
real Python identifiers — `str.isidentifier()` accepts every one — and real error
strings of the kind a user pastes into `repoatlas debug`.

It lives in `common-lib` rather than `python-service` deliberately. This file
exists to exercise character handling, and any extra file added to a repository
shifts that repository's semantic neighbourhood; putting it beside
`python-service/src/config.py` moved an unrelated hostname question's ground-truth
answer from rank 5 to rank 6. A fixture for one capability should not quietly
retune the measurement of another.
"""

NOM_INVALIDE = "le nom fourni est invalide"
JP_VALIDATION_ERROR = "名前が正しくありません"
RU_VALIDATION_ERROR = "Указано неверное имя"


def créer_utilisateur(nom: str) -> dict[str, str]:
    """Create a user record. Named in French on purpose."""
    if not nom:
        raise ValueError(NOM_INVALIDE)
    return {"nom": nom}


class Ünïcode:
    """A class whose name carries diacritics."""

    def traiter(self, nom: str) -> str:
        return créer_utilisateur(nom)["nom"]
