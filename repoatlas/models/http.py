"""HTTP route, deployment and endpoint identity.

Three concepts, kept apart because each proves something different:

* an **HTTP route** is a method and a path — ``GET /hello/me``. Code that declares
  or calls it says nothing about which host serves it;
* an **HTTP deployment** is a statically declared base location —
  ``https://api.example.com/v1``. A repository *declaring* one is not evidence
  that anything is running there;
* an **HTTP endpoint** is a method and an absolute URL —
  ``GET https://api.example.com/v1/users``. Two repositories meeting on one is the
  strongest statement this feature makes: both statically name the same complete
  operation. It is still a statement about source text, not about a network.

Everything here is pure and stdlib-only, because three layers must agree on it
byte for byte: the parsers normalize what they extract, the linker re-validates
what it interns, and query analysis spells the exact keys a question names. A
normalization computed three slightly different ways would be three identities.

**Normalization is deliberately conservative.** Two spellings share an identity
only where URL semantics make them the same thing: the scheme and host are
case-insensitive, so they are lowercased, and the query and fragment are not part
of which operation a request names, so they are dropped. Nothing else is folded —
not path case, not a trailing slash, not a default port, not ``http`` against
``https``, not a route parameter against a concrete value. Every one of those
could be equivalent on some server and is not on another, and a false link is
worse than a missed one. Anything this module cannot read with certainty —
credentials in the authority, a non-ASCII host, a server variable — is rejected
outright rather than approximated.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

#: The operations an OpenAPI path item may declare, which is also the set every
#: extractor maps onto. Anything else — ``CONNECT``, a custom verb, a typo — has
#: no identity here.
HTTP_METHODS: tuple[str, ...] = (
    "GET",
    "PUT",
    "POST",
    "DELETE",
    "OPTIONS",
    "HEAD",
    "PATCH",
    "TRACE",
)

ROUTE_KEY_PREFIX = "http-route:"
DEPLOYMENT_KEY_PREFIX = "http-deployment:"
ENDPOINT_KEY_PREFIX = "http-endpoint:"

#: Longer than any real route or base URL, and short enough that a generated or
#: adversarial literal cannot become a pathological key.
MAX_IDENTITY_LENGTH = 2048

#: The most keys one question may generate. A bare path fans out across every
#: method, so the bound is on the product rather than on the tokens.
MAX_QUERY_KEYS = 64

_SCHEMES = frozenset({"http", "https"})
_LABEL = r"[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?"
_HOSTNAME = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
_IPV6 = re.compile(r"\[[0-9a-f:.]+\]")
_PORT = re.compile(r"[1-9][0-9]{0,4}")

#: Printable ASCII other than space. Anything outside it — whitespace, a control
#: character, a non-ASCII letter — means the literal is not a plain path, and
#: guessing its encoded form would be inventing one.
_PRINTABLE = frozenset(chr(code) for code in range(0x21, 0x7F))
_NOT_IN_PATH = frozenset({"\\", "?", "#"})


def http_method(value: str) -> str | None:
    """``value`` when it is exactly one of :data:`HTTP_METHODS`, else ``None``.

    Exact, uppercase and unfolded on purpose: each extractor knows how its own
    library spells a method (``app.get`` is GET; ``fetch`` upper-cases some names
    and not others) and maps it here explicitly, so this function never has to
    guess on a library's behalf.
    """
    return value if value in HTTP_METHODS else None


def route_path(value: str) -> str | None:
    """A declared route path, byte-exact, or ``None`` when it is not one.

    It must begin with ``/`` and consist of printable ASCII without ``\\``, ``?``
    or ``#``. Everything else is kept as written — case, a trailing slash, ``//``,
    percent-encoding, and parameter templates such as ``{id}``, ``:id`` or
    ``<int:id>``, which are literal text here and match only an identical
    spelling. A route key is never a pattern; matching a client's path *template*
    against declared routes is the separate, narrower rule of
    :func:`template_matches`.
    """
    if not value.startswith("/") or len(value) > MAX_IDENTITY_LENGTH:
        return None
    if any(char not in _PRINTABLE or char in _NOT_IN_PATH for char in value):
        return None
    return value


@dataclass(frozen=True, slots=True)
class HttpTarget:
    """Where a statically known request goes.

    ``origin`` is ``scheme://host[:port]`` for an absolute target and ``None`` for
    a relative one, which names a route and nothing about where it is served.
    """

    path: str
    origin: str | None = None

    @property
    def url(self) -> str | None:
        return None if self.origin is None else f"{self.origin}{self.path}"


@dataclass(frozen=True, slots=True)
class _Absolute:
    origin: str
    path: str
    #: Whether the literal carried a query or fragment, which is dropped from an
    #: endpoint but disqualifies a base URL (see :func:`deployment_base`).
    trailing: bool


def _absolute(value: str) -> _Absolute | None:
    """Parse an absolute ``http``/``https`` URL, or refuse it.

    Standards-based through :func:`urllib.parse.urlsplit`, then checked more
    strictly than the parser itself, because ``urlsplit`` is permissive by design
    and an identity must not be.
    """
    if len(value) > MAX_IDENTITY_LENGTH or any(char not in _PRINTABLE for char in value):
        return None
    if "\\" in value:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    scheme = parts.scheme
    if scheme not in _SCHEMES or not value.lower().startswith(f"{scheme}://"):
        return None
    netloc = parts.netloc
    # Userinfo is refused, not stripped: a URL carrying credentials must not
    # become an identity, and quietly dropping them would still promote the rest
    # of a literal someone put a secret in.
    if not netloc or "@" in netloc:
        return None

    if netloc.startswith("["):
        close = netloc.find("]")
        if close < 0:
            return None
        host, rest = netloc[: close + 1].lower(), netloc[close + 1 :]
        if not _IPV6.fullmatch(host):
            return None
        if rest and not rest.startswith(":"):
            return None
        port = rest[1:] if rest else None
    else:
        host, colon, port_text = netloc.partition(":")
        host = host.lower()
        if not _HOSTNAME.fullmatch(host):
            return None
        port = port_text if colon else None

    if port is not None and (not _PORT.fullmatch(port) or int(port) > 65535):
        return None

    path = route_path(parts.path or "/")
    if path is None:
        return None
    origin = f"{scheme}://{host}" + (f":{port}" if port is not None else "")
    return _Absolute(
        origin=origin,
        path=path,
        trailing=bool(parts.query or parts.fragment or "?" in value or "#" in value),
    )


def client_target(value: str) -> HttpTarget | None:
    """The target of a statically known client call, or ``None``.

    A relative target must begin with a single ``/``: ``//host/x`` is
    protocol-relative and names a host this cannot see, and ``hello`` resolves
    against a document nobody here can read. The query and fragment are dropped in
    both forms — they do not change which operation is requested, and a token in
    a query string must never reach an identity.
    """
    if value.startswith("/"):
        if value.startswith("//"):
            return None
        cut = min((i for i in (value.find("?"), value.find("#")) if i >= 0), default=len(value))
        path = route_path(value[:cut])
        return HttpTarget(path=path) if path is not None else None
    parsed = _absolute(value)
    if parsed is None:
        return None
    return HttpTarget(path=parsed.path, origin=parsed.origin)


def deployment_base(value: str) -> str | None:
    """A statically declared base URL in canonical form, or ``None``.

    ``scheme://host[:port]`` plus the base path with **one** trailing ``/``
    removed, because a base is appended to rather than resolved against:
    ``https://x/`` and ``https://x`` compose every route identically. Refused
    outright: a server variable such as ``{environment}`` (resolving it is not
    offered), a relative base, and a base carrying a query or fragment, which has
    no append semantics at all.
    """
    if "{" in value or "}" in value:
        return None
    parsed = _absolute(value)
    if parsed is None or parsed.trailing:
        return None
    path = parsed.path[:-1] if parsed.path.endswith("/") else parsed.path
    return parsed.origin + path


def swagger2_base(scheme: str, host: str, base_path: str) -> str | None:
    """A Swagger 2 base — ``scheme://host`` + ``basePath`` — in canonical form, or ``None``.

    The one rule every Swagger 2 source shares, so that a specification document and a
    template written in code compose the same identity from the same three fields. The
    host must be a bare authority (no path, credentials, query, fragment or template)
    and the base path rooted. No ``host`` at all is the caller's to refuse: the API is
    then served wherever the document itself was fetched from.
    """
    if scheme not in _SCHEMES or not host.strip() or any(char in host for char in "/@?#{}\\"):
        return None
    if not base_path.startswith("/"):
        return None
    return deployment_base(f"{scheme}://{host}{base_path}")


def compose(deployment: str, path: str) -> str:
    """The absolute URL a route has under a deployment: literal append.

    Not :func:`urllib.parse.urljoin`, which *resolves* ``/users`` against
    ``https://x/v1`` to ``https://x/users`` and drops the base path. OpenAPI
    appends the path to the server URL, and so does every framework prefix this
    feature reads.
    """
    return f"{deployment}{path}"


def route_body(method: str, path: str) -> str:
    """``GET /hello/me``: the route's display name and the parser's target key."""
    return f"{method} {path}"


def endpoint_body(method: str, url: str) -> str:
    """``GET https://api.example.com/hello/me``."""
    return f"{method} {url}"


def route_key(body: str) -> str:
    method, _, path = body.partition(" ")
    return f"{ROUTE_KEY_PREFIX}{method}:{path}"


def deployment_key(body: str) -> str:
    return f"{DEPLOYMENT_KEY_PREFIX}{body}"


def endpoint_key(body: str) -> str:
    method, _, url = body.partition(" ")
    return f"{ENDPOINT_KEY_PREFIX}{method}:{url}"


def canonical_route(body: str) -> str | None:
    """``body`` if it is exactly a canonical route, else ``None``."""
    method, space, path = body.partition(" ")
    if not space or http_method(method) is None or route_path(path) != path:
        return None
    return body


def canonical_deployment(body: str) -> str | None:
    """``body`` if it is exactly a canonical deployment, else ``None``."""
    return body if body and deployment_base(body) == body else None


def canonical_endpoint(body: str) -> str | None:
    """``body`` if it is exactly a canonical endpoint, else ``None``."""
    method, space, url = body.partition(" ")
    if not space or http_method(method) is None:
        return None
    parsed = _absolute(url)
    if parsed is None or parsed.trailing or f"{parsed.origin}{parsed.path}" != url:
        return None
    return body


# -- route templates -----------------------------------------------------------
#
# A client often writes its path as a template — ``/v1/courses/${courseId}/roster``
# — which names no route key at all. What it does name is a *shape*: fixed
# segments, and positions one value fills. Matching that shape against the routes
# a server declares is structural and deliberately narrow:
#
# * a substitution is a dynamic segment only when it is the whole segment, and it
#   fills exactly one segment. That is what "structural" means here, stated rather
#   than hidden: a value containing ``/`` is invisible to static reading;
# * a dynamic segment matches only a whole server parameter — ``:id``, ``{id}``,
#   ``<id>``, ``<int:id>`` — never a literal, so ``/users/42`` still does not
#   become ``/users/:id``, and a wildcard, a regex or an optional parameter matches
#   nothing;
# * literal segments compare byte for byte, like every route here.
#
# A template that cannot be read this way is refused with a reason code, which a
# caller reports rather than approximates.

#: Kinds of template part, as parsers serialize them: literal text, and a
#: substitution with its (bounded) source text.
TEMPLATE_LITERAL = "l"
TEMPLATE_SUBSTITUTION = "s"
#: A substitution the parser proved is empty or begins a query string —
#: ``${params ? `?${params}` : ""}`` — so it ends the path and adds nothing to it.
TEMPLATE_QUERY = "q"

#: Why a template was refused. Stable codes: callers surface them verbatim.
NOT_A_PATH = "not_a_path"
MIXED_SEGMENT = "mixed_segment"
GLUED_SUBSTITUTION = "glued_substitution"
NO_LITERAL_SEGMENT = "no_literal_segment"
ESCAPE_SEQUENCE = "escape_sequence"
TRUNCATED = "truncated"

#: A whole server parameter segment, in the spellings the supported frameworks
#: and OpenAPI declare. Anything with a modifier (``:id?``, ``:id(\\d+)``) or a
#: wildcard is not one: it may match more, or less, than one value.
_PARAMETER = re.compile(r":[A-Za-z_$][\w$]*|\{[A-Za-z_][\w.\-]*\}|<(?:[A-Za-z_]\w*:)?[A-Za-z_]\w*>")


def is_parameter_segment(segment: str) -> bool:
    """Whether one path segment is exactly a declared route parameter."""
    return _PARAMETER.fullmatch(segment) is not None


@dataclass(frozen=True, slots=True)
class RouteTemplate:
    """A request path read as segments, some of which one value fills.

    ``segments`` holds each segment after the leading ``/``: its literal text, or
    ``None`` where a substitution fills it. ``expressions`` holds the source text
    of those substitutions in order — for display only, never for matching.
    """

    segments: tuple[str | None, ...]
    expressions: tuple[str, ...] = ()
    #: Whether a query string, or a tail proven empty-or-query, followed the path.
    query: bool = False

    @property
    def dynamic(self) -> bool:
        return any(segment is None for segment in self.segments)

    @property
    def path(self) -> str | None:
        """The literal path when no segment is dynamic, else ``None``."""
        literal: list[str] = []
        for segment in self.segments:
            if segment is None:
                return None
            literal.append(segment)
        return "/" + "/".join(literal)

    @property
    def display(self) -> str:
        """``/v1/courses/${courseId}/roster``: what the source wrote, path only."""
        expressions = iter(self.expressions)
        return "/" + "/".join(
            segment if segment is not None else "${" + next(expressions, "") + "}"
            for segment in self.segments
        )


def _cut(text: str) -> int | None:
    """Where a query or fragment begins in literal text, if it does."""
    found = [index for index in (text.find("?"), text.find("#")) if index >= 0]
    return min(found) if found else None


def route_template(
    parts: Sequence[Sequence[str]], *, truncated: bool = False
) -> RouteTemplate | str:
    """Read a template's parts as a route shape, or return why it cannot be read.

    ``parts`` is the parser-neutral form: ``[kind, text]`` pairs of kinds
    :data:`TEMPLATE_LITERAL`, :data:`TEMPLATE_SUBSTITUTION` and
    :data:`TEMPLATE_QUERY`. The rules, each a refusal rather than a guess:

    * the path starts with one literal ``/``: a leading substitution is a base
      being composed, and ``//`` names a host (:data:`NOT_A_PATH`);
    * a literal ``?`` or ``#`` ends the path, and so does a proven query tail; a
      trailing substitution that is *not* proven is glued to the last segment
      (:data:`GLUED_SUBSTITUTION`), as ``roster${query(f)}`` is;
    * a substitution sharing its segment with anything else is
      :data:`MIXED_SEGMENT` — ``x-${y}``, ``${a}.csv``, ``${a}${b}``;
    * a template with no literal segment constrains nothing
      (:data:`NO_LITERAL_SEGMENT`);
    * an escape sequence would need decoding to be read (:data:`ESCAPE_SEQUENCE`),
      and a template the parser had to cut short (:data:`TRUNCATED`) could match a
      route it does not name.
    """
    if truncated:
        return TRUNCATED
    if not parts or parts[0][0] != TEMPLATE_LITERAL:
        return NOT_A_PATH
    head = parts[0][1] if len(parts[0]) > 1 else ""
    if not head.startswith("/") or head.startswith("//"):
        return NOT_A_PATH

    # One entry per segment, the text before the first "/" included; each piece is
    # literal text, or None for a substitution.
    segments: list[list[str | None]] = [[]]
    expressions: list[str] = []
    query = False
    for index, part in enumerate(parts):
        kind = part[0]
        value = part[1] if len(part) > 1 else ""
        if kind == TEMPLATE_LITERAL:
            if "\\" in value:
                return ESCAPE_SEQUENCE
            cut = _cut(value)
            text = value if cut is None else value[:cut]
            if any(char not in _PRINTABLE for char in text):
                return NOT_A_PATH
            pieces = text.split("/")
            if pieces[0]:
                segments[-1].append(pieces[0])
            segments.extend([piece] if piece else [] for piece in pieces[1:])
            if cut is not None:
                query = value[cut] == "?"
                break
        elif kind == TEMPLATE_QUERY:
            for rest in parts[index + 1 :]:
                if rest[0] != TEMPLATE_LITERAL or (len(rest) > 1 and rest[1]):
                    return GLUED_SUBSTITUTION
            query = True
            break
        elif kind == TEMPLATE_SUBSTITUTION:
            segments[-1].append(None)
            expressions.append(value)
        else:
            return NOT_A_PATH

    shaped: list[str | None] = []
    real = segments[1:]
    for position, pieces in enumerate(real):
        if not pieces:
            shaped.append("")
        elif pieces == [None]:
            shaped.append(None)
        elif all(piece is not None for piece in pieces):
            shaped.append("".join(piece for piece in pieces if piece is not None))
        else:
            tail = position == len(real) - 1 and pieces[-1] is None
            glued = tail and all(piece is not None for piece in pieces[:-1])
            return GLUED_SUBSTITUTION if glued else MIXED_SEGMENT

    template = RouteTemplate(segments=tuple(shaped), expressions=tuple(expressions), query=query)
    if template.dynamic and not any(segment for segment in shaped):
        return NO_LITERAL_SEGMENT
    if len(template.display) > MAX_IDENTITY_LENGTH:
        return NOT_A_PATH
    literal = template.path
    if literal is not None and route_path(literal) != literal:
        return NOT_A_PATH
    return template


def template_matches(template: RouteTemplate, path: str) -> bool:
    """Whether a declared route ``path`` has ``template``'s shape.

    Same number of segments; every literal segment byte-equal; every dynamic
    segment against a whole declared parameter. Nothing else matches — above all
    not a literal against a parameter, in either direction.
    """
    if not path.startswith("/"):
        return False
    declared = path[1:].split("/")
    if len(declared) != len(template.segments):
        return False
    for mine, theirs in zip(template.segments, declared, strict=True):
        if mine is None:
            if not is_parameter_segment(theirs):
                return False
        elif mine != theirs:
            return False
    return True


# -- routes named in prose -----------------------------------------------------

#: One segment as a sentence may write it: literal text, a declared parameter in
#: any of the four spellings, or a client template's substitution.
_MENTION_SEGMENT = r"(?:\$\{[^/{}\s]*\}|\{[^/{}\s]+\}|<[^/<>\s]+>|:[A-Za-z_$][\w$]*|[\w.\-~%]+)"
#: An optional method, then a rooted path of such segments.
_MENTION = re.compile(rf"(?:\b({'|'.join(HTTP_METHODS)})\s+)?((?:/{_MENTION_SEGMENT})+)")
#: Sentence punctuation that may end a mention without belonging to it.
_MENTION_TRAILING = ".,;:!?)]'\"`"


@dataclass(frozen=True, slots=True)
class RouteMention:
    """A route a sentence names, and where its path sits in that sentence.

    The span is the **path** alone, because the method carries no character the
    file-path reading could trip over. ``shape`` is what anchoring compares: a
    segment is either the literal text written, or ``None`` where one value fills
    it — which is the same reading :func:`template_matches` applies to a declared
    route, so a mention, a client template and a handler's path are all compared
    as the same kind of thing.
    """

    method: str | None
    path: str
    start: int
    end: int

    @property
    def shape(self) -> tuple[str | None, ...]:
        return route_shape(self.path)


def route_shape(path: str) -> tuple[str | None, ...]:
    """``/v1/courses/:id/roster`` → ``("v1", "courses", None, "roster")``.

    A literal never becomes ``None`` and ``None`` never becomes a literal, so
    ``/v1/users/42`` and ``/v1/users/:id`` are different shapes — the distinction
    a supplied route must keep to be worth anchoring against.
    """
    if not path.startswith("/"):
        return ()
    segments: list[str | None] = []
    for segment in path[1:].split("/"):
        substitution = segment.startswith("${") and segment.endswith("}")
        segments.append(None if substitution or is_parameter_segment(segment) else segment)
    return tuple(segments)


def route_mentions(text: str) -> tuple[RouteMention, ...]:
    """Every route ``text`` names, in the order it names them.

    Detection only: that a sentence *names* a route says nothing about whether the
    route was supplied, which is a separate question with a separate answer. A
    mention may carry a method or not, and trailing sentence punctuation is left
    to the sentence.
    """
    found: list[RouteMention] = []
    for match in _MENTION.finditer(text):
        path, start = match.group(2), match.start(2)
        trimmed = path.rstrip(_MENTION_TRAILING)
        # A path that was only punctuation after its slash is not a route.
        if not trimmed or trimmed.endswith("/"):
            continue
        method = match.group(1)
        found.append(
            RouteMention(
                method=method.upper() if method else None,
                path=trimmed,
                start=start,
                end=start + len(trimmed),
            )
        )
    return tuple(found)


def same_shape(mention: str, supplied: str) -> bool:
    """Whether two route paths are the same shape, parameter names ignored."""
    shape = route_shape(mention)
    return bool(shape) and shape == route_shape(supplied)


# -- query analysis ------------------------------------------------------------

_METHOD_ALTERNATION = "|".join(HTTP_METHODS)
#: ``GET /users`` or ``get https://api.example.com/users`` inside a question.
_METHOD_TARGET = re.compile(rf"(?<![\w/])({_METHOD_ALTERNATION})\s+(\S+)", re.IGNORECASE)
#: A bare absolute URL or path, not already part of a longer token.
_BARE_TARGET = re.compile(r"(?<![\w/:.])(https?://\S+|/\S+)", re.IGNORECASE)
#: Punctuation that belongs to the sentence around a target rather than to it.
_TRAILING = ".,;:!?)]}>'\"`"
_LEADING = "(['\"`<"


def _clean(token: str) -> str:
    return token.lstrip(_LEADING).rstrip(_TRAILING)


def query_keys(question: str) -> tuple[str, ...]:
    """The exact HTTP keys a question spells out, for exact entity lookup.

    ``GET /users`` names one route; ``GET https://x/users`` names one endpoint. A
    bare path or URL names no method, so it is looked up under every method — each
    lookup is still exact, and the method stays part of whatever identity is
    found. A bare URL is also looked up as a deployment base.

    Nothing here is fuzzy: a key is produced only from a target that normalizes
    under the same rules the index was built with, and a key that matches nothing
    finds nothing.
    """
    keys: dict[str, None] = {}
    consumed: list[tuple[int, int]] = []

    for match in _METHOD_TARGET.finditer(question):
        method = match.group(1).upper()
        target = client_target(_clean(match.group(2)))
        if target is None:
            continue
        consumed.append(match.span(2))
        if target.url is not None:
            keys.setdefault(endpoint_key(endpoint_body(method, target.url)), None)
        else:
            keys.setdefault(route_key(route_body(method, target.path)), None)

    for match in _BARE_TARGET.finditer(question):
        start, end = match.span(1)
        if any(start >= lo and end <= hi for lo, hi in consumed):
            continue
        raw = _clean(match.group(1))
        target = client_target(raw)
        if target is None:
            continue
        if target.url is not None:
            for method in HTTP_METHODS:
                keys.setdefault(endpoint_key(endpoint_body(method, target.url)), None)
            base = deployment_base(raw)
            if base is not None:
                keys.setdefault(deployment_key(base), None)
        else:
            for method in HTTP_METHODS:
                keys.setdefault(route_key(route_body(method, target.path)), None)

    return tuple(keys)[:MAX_QUERY_KEYS]
