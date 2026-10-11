"""The admin UI's Runtime panel: wiring and the permissive-auth banner.

``GET /api/v1/runtime`` (see ``test_runtime_route.py``) is already proven to
report the process's real auth posture and never leak a secret. This file
pins the three things only the page itself can get wrong:

1. The Settings view actually calls ``loadRuntime()`` when it is opened --
   a route that exists but nothing ever fetches is not a panel.
2. The warning banner renders exactly when ``auth_mode_permissive`` is
   true, and is cleared otherwise -- a banner that never goes away once
   shown is as dishonest as one that never appears.
3. The panel renders straight from the response's own fields, with no
   second, hardcoded copy of what a permissive mode means.
"""

from __future__ import annotations

import re
from pathlib import Path

import trellis_api

INDEX_HTML = Path(trellis_api.__file__).parent / "static" / "index.html"


def _page() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _between(page: str, start: str, end: str) -> str:
    found = re.search(re.escape(start) + r"(.*?)" + re.escape(end), page, re.DOTALL)
    assert found is not None, f"could not find region between {start!r} and {end!r}"
    return found.group(1)


def test_opening_settings_loads_the_runtime_panel() -> None:
    page = _page()
    view_switch = _between(page, "if (name === 'settings') {", "\n")
    assert "loadRuntime()" in view_switch
    assert "loadSettings()" in view_switch


def test_banner_fires_only_when_auth_mode_is_permissive() -> None:
    page = _page()
    body = _between(
        page, "function renderAuthBanner(data) {", "function formatRuntimeDir("
    )
    assert "!data || !data.auth_mode_permissive" in body
    assert "el.innerHTML = '';" in body
    assert "degraded-banner" in body


def test_banner_names_the_fix_not_just_the_problem() -> None:
    """A warning with no remedy just raises anxiety -- the banner must
    name the env var an operator would actually set.
    """
    page = _page()
    body = _between(
        page, "function renderAuthBanner(data) {", "function formatRuntimeDir("
    )
    assert "TRELLIS_AUTH_MODE=required" in body


def test_banner_claim_is_exact_per_auth_mode() -> None:
    """``optional`` grants every scope only to a credential-less request --
    a request bearing an *invalid* key still gets 401, and a valid scoped
    key gets only its own scopes (see ``trellis_api/auth.py::authenticate``).
    The blanket "every request is granted every scope" claim is true only
    for ``off``; ``optional`` needs its own, narrower wording.
    """
    page = _page()
    body = _between(
        page, "function renderAuthBanner(data) {", "function formatRuntimeDir("
    )
    assert "mode === 'optional'" in body
    assert "a request without an API key is granted every scope" in body
    assert "every request is granted every scope" in body


def test_banner_claim_and_detail_ternaries_are_each_keyed_on_optional() -> None:
    """Pins the *structure* of the ``claim``/``detail`` ternaries, not just
    substring presence (re-gate #867 S2): the substring-only test above
    passes even if a mutant swaps which text goes with which mode (M22),
    or keys ``detail`` on ``mode === 'off'`` instead of ``mode ===
    'optional'`` (M23) -- both mutants still contain every substring
    *somewhere* in the function body. Extracting each ternary's condition
    and both branches by name catches either change directly.
    """
    page = _page()
    body = _between(
        page, "function renderAuthBanner(data) {", "function formatRuntimeDir("
    )

    claim_match = re.search(
        r"const claim = mode === 'optional'\s*\?\s*'([^']*)'\s*:\s*'([^']*)';",
        body,
    )
    assert claim_match is not None, body
    assert claim_match.group(1) == "a request without an API key is granted every scope"
    assert claim_match.group(2) == "every request is granted every scope"

    detail_match = re.search(
        r"const detail = mode === 'optional'\s*\?\s*'([^']*)'\s*:\s*'([^']*)';",
        body,
    )
    assert detail_match is not None, body
    assert detail_match.group(1).startswith(
        "A caller that omits its API key right now is granted every scope."
    )
    assert detail_match.group(2).startswith("No API key is required right now.")


def test_runtime_panel_reads_fields_from_the_response_not_a_copy() -> None:
    page = _page()
    body = _between(
        page, "function renderRuntimePanel(data) {", "async function loadRuntime() {"
    )
    for field in (
        "data.auth_mode",
        "data.auth_mode_permissive",
        "data.ops_detail",
        "data.metrics_public",
        "data.bind_host",
        "data.bind_port",
        "data.embedder",
        "data.learning_artifacts_dir",
        "data.host_jobs_dir",
        "data.stores",
        "data.llm_routes",
        "data.llm_routes_error",
    ):
        assert field in body, f"{field} not read from the /runtime response"
