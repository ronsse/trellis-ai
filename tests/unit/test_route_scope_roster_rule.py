"""Every FastAPI route under ``/api/v1`` (plus the two unversioned probe
groups) carries a required-scope set this file rostered by hand.

FastAPI's ``include_router`` merges three things into the scope dependency
a route actually enforces at dispatch time: the parent router's own
``dependencies``, the ``dependencies=`` kwarg passed to ``include_router``,
and the specific route's own ``dependencies=`` kwarg (see
``fastapi.routing._EffectiveRouteContext.from_api_route``, which builds
``dependencies=[*include_context.dependencies, *route.dependencies]``).
This file reconstructs that same merge directly from
``fastapi.routing._IncludedRouter.include_context`` and each
``APIRoute.dependencies``, rather than through FastAPI's lazy
effective-route machinery (version-specific internal API), and recovers
the scope each ``Depends(require_scope(scope))`` instance checks by
reading the ``scope`` free variable off its closure cell.

The roster below is split in two, matching how ``trellis_api.app.create_app``
documents it:

- ``ROUTER_SCOPE_ROSTER`` -- the scope every route under a given router tag
  requires at minimum, hand-read from the router -> scope comment block in
  ``create_app``.
- ``ROUTE_OVERRIDE_ROSTER`` -- the *additional* scope a handful of specific
  routes require on top of their router's baseline (observations' two
  POSTs, explore's four pack-telemetry GETs, policies' POST/DELETE,
  classify's mine/apply/revoke/dismiss), hand-read from each route's own
  ``dependencies=[Depends(require_scope(...))]`` kwarg in its route
  module.

``UNAUTHENTICATED_ROUTERS`` names the two router tags with no scope
dependency at all (version handshake, liveness/readiness probes) and the
substring that must appear in a code comment near their
``app.include_router(...)`` call, so an unauthenticated route can never be
silent about why.

The population is pinned by :func:`tests.ast_rules.assert_hand_read_floor`
at 76 -- every ``(method, path)`` wired through one of the 14 routers
``create_app`` includes. The bare root redirect (``GET /``) and FastAPI's
own ``/docs``/``/openapi.json``/``/redoc`` routes are excluded: they carry
no application data and are not wired through any of our routers' scope
map. The static ``/ui`` mount is excluded for the same reason -- it serves
the page itself unauthenticated by design (see the comment above its
``app.mount`` call); every *data* request the page makes still goes
through a rostered ``/api/v1`` route with its own API key.

Mutation-tested: dropping ``dependencies=admin_auth`` from one router's
``include_router`` call, or dropping a route's own
``dependencies=[Depends(require_scope(...))]`` override, each lower that
route's live scope set below its roster entry and fail
``test_every_route_scope_matches_its_roster`` for exactly the routes under
that router or at that path -- see the mutation-testing note shipped with
the pull request that added this file. Adding a wholly new, unrostered
router tag fails the same test via its "router tag is not rostered"
branch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.routing import (
    APIRoute,
    _IncludedRouter,
)

import trellis_api.app as app_mod
from tests.ast_rules import assert_hand_read_floor
from trellis.auth import SCOPE_ADMIN, SCOPE_INGEST, SCOPE_MUTATE, SCOPE_READ
from trellis_api.app import create_app

#: Router tag -> the scope every route under it requires at minimum.
ROUTER_SCOPE_ROSTER: dict[str, frozenset[str]] = {
    "admin": frozenset({SCOPE_ADMIN}),
    "settings": frozenset({SCOPE_ADMIN}),
    "ingest": frozenset({SCOPE_INGEST}),
    "retrieve": frozenset({SCOPE_READ}),
    "explore": frozenset({SCOPE_READ}),
    "analyze": frozenset({SCOPE_ADMIN}),
    "curate": frozenset({SCOPE_MUTATE}),
    "mutations": frozenset({SCOPE_MUTATE}),
    "policies": frozenset({SCOPE_READ}),
    "extract": frozenset({SCOPE_MUTATE}),
    "observations": frozenset({SCOPE_READ}),
    "classify": frozenset({SCOPE_READ}),
}

#: (method, path) -> the scope that route's own ``dependencies=`` kwarg
#: additionally requires, on top of its ``ROUTER_SCOPE_ROSTER`` entry.
ROUTE_OVERRIDE_ROSTER: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/api/v1/observations"): frozenset({SCOPE_MUTATE}),
    ("POST", "/api/v1/measurements"): frozenset({SCOPE_MUTATE}),
    ("GET", "/api/v1/events"): frozenset({SCOPE_ADMIN}),
    ("GET", "/api/v1/events/{event_id}"): frozenset({SCOPE_ADMIN}),
    ("GET", "/api/v1/packs"): frozenset({SCOPE_ADMIN}),
    ("GET", "/api/v1/packs/{pack_id}"): frozenset({SCOPE_ADMIN}),
    ("POST", "/api/v1/policies"): frozenset({SCOPE_ADMIN}),
    ("DELETE", "/api/v1/policies/{policy_id}"): frozenset({SCOPE_ADMIN}),
    ("POST", "/api/v1/classify/candidates/mine"): frozenset({SCOPE_ADMIN}),
    ("POST", "/api/v1/classify/rules"): frozenset({SCOPE_ADMIN}),
    ("DELETE", "/api/v1/classify/rules/{kind}/{key}"): frozenset({SCOPE_ADMIN}),
    ("POST", "/api/v1/classify/candidates/{candidate_id}/dismiss"): frozenset(
        {SCOPE_ADMIN}
    ),
}

#: Router tag -> a substring that must appear in a comment near its
#: ``app.include_router(...)`` call, explaining why it carries no scope.
UNAUTHENTICATED_ROUTERS: dict[str, str] = {
    "version": "unauthenticated",
    "health": "unauthenticated",
}

#: Hand-read population: distinct (method, path) pairs wired through the
#: 14 routers create_app() includes, counted from trellis_api/app.py and
#: the route decorators in trellis_api/routes/*.py.
_HAND_READ_ROUTE_COUNT = 76


def _scope_from_depends(dep: Any) -> str | None:
    """Recover the scope a ``Depends(require_scope(scope))`` instance

    checks, by reading the ``scope`` free variable off its closure.
    ``require_scope`` is a dependency *factory*: each call returns a
    fresh closure named ``_require`` with exactly one free variable,
    ``scope``. That closure cell is the only place the scope lives once
    the factory call has returned -- this reads it back rather than
    re-deriving it some other way, so a change to which scope a given
    ``Depends(...)`` enforces is caught whether it came from ``app.py``'s
    router map or a route module's own override.
    """
    fn = getattr(dep, "dependency", None)
    code = getattr(fn, "__code__", None)
    closure = getattr(fn, "__closure__", None)
    if fn is None or code is None or closure is None:
        return None
    if getattr(fn, "__name__", None) != "_require" or "scope" not in code.co_freevars:
        return None
    return closure[code.co_freevars.index("scope")].cell_contents


def _collect_routes() -> dict[tuple[str, str], tuple[str, frozenset[str]]]:
    """(method, path) -> (router tag, effective required-scope set) for

    every route wired through one of ``create_app()``'s ``include_router``
    calls, merging the inclusion's own dependencies with each route's own
    override exactly as FastAPI does at dispatch time (see module
    docstring).
    """
    app = create_app()
    routes: dict[tuple[str, str], tuple[str, frozenset[str]]] = {}
    for entry in app.routes:
        if not isinstance(entry, _IncludedRouter):
            continue
        ctx = entry.include_context
        tag = ctx.tags[0] if ctx.tags else ""
        router_scopes = frozenset(
            scope
            for dep in ctx.dependencies
            if (scope := _scope_from_depends(dep)) is not None
        )
        for route in entry.original_router.routes:
            if not isinstance(route, APIRoute):
                continue
            route_scopes = frozenset(
                scope
                for dep in route.dependencies
                if (scope := _scope_from_depends(dep)) is not None
            )
            full_path = ctx.prefix + route.path
            for method in sorted(route.methods - {"HEAD"}):
                routes[(method, full_path)] = (tag, router_scopes | route_scopes)
    return routes


def test_route_population_floor() -> None:
    """Pin the route count a person counted, so a scan that silently

    narrows (e.g. a routers list edited to skip one) cannot shrink the
    population this file checks without the suite noticing.
    """
    assert_hand_read_floor(
        len(_collect_routes()),
        _HAND_READ_ROUTE_COUNT,
        subject="(method, path) routes wired through create_app()'s routers",
        hint=(
            "count app.include_router(...) calls' routers' route "
            "decorators by hand in trellis_api/app.py and trellis_api/"
            "routes/*.py."
        ),
    )


def test_every_route_scope_matches_its_roster() -> None:
    mismatches: list[str] = []
    for (method, path), (tag, scopes) in sorted(_collect_routes().items()):
        if tag in UNAUTHENTICATED_ROUTERS:
            if scopes:
                mismatches.append(
                    f"{method} {path}: router {tag!r} is rostered as "
                    f"unauthenticated but requires {sorted(scopes)}"
                )
            continue
        if tag not in ROUTER_SCOPE_ROSTER:
            mismatches.append(
                f"{method} {path}: router tag {tag!r} is not rostered "
                "in ROUTER_SCOPE_ROSTER or UNAUTHENTICATED_ROUTERS"
            )
            continue
        expected = ROUTER_SCOPE_ROSTER[tag] | ROUTE_OVERRIDE_ROSTER.get(
            (method, path), frozenset()
        )
        if scopes != expected:
            mismatches.append(
                f"{method} {path}: roster expects {sorted(expected)}, "
                f"live dependencies require {sorted(scopes)}"
            )
    assert not mismatches, "routes whose live scope disagrees with the roster:\n" + (
        "\n".join(mismatches)
    )


def test_unauthenticated_routers_carry_a_written_reason() -> None:
    """Every router in ``UNAUTHENTICATED_ROUTERS`` has a comment naming

    its reason within a few lines of its ``app.include_router(...)``
    call -- an unauthenticated route can be correct, but never silent
    about why.
    """
    source_path = Path(app_mod.__file__)
    lines = source_path.read_text().splitlines()
    for tag, needle in UNAUTHENTICATED_ROUTERS.items():
        call_idx = next(
            i
            for i, line in enumerate(lines)
            if "include_router" in line and f"{tag}.router" in line
        )
        window = "\n".join(lines[max(0, call_idx - 8) : call_idx]).lower()
        assert needle in window, (
            f"no comment within 8 lines above {tag}.router's "
            f"include_router mentions {needle!r} in {source_path}"
        )
