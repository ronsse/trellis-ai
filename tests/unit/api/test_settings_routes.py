"""Tests for the admin-scoped ``/api/v1/settings`` routes (PR 3).

Mirrors ``tests/unit/api/test_auth.py``'s fixture pattern exactly (a
fresh in-memory ``StoreRegistry`` bound to ``trellis_api.app``, and a
``TestClient`` that is never entered as a context manager so the
lifespan never re-inits ``_registry`` out from under the fixture).

Covers: 401/403 without the admin scope, the GET catalog shape for
every registered setting, PUT/POST success through the real
``Operation.SETTINGS_UPDATE`` governed path, refusals (unknown name,
wrong type, out of range, degraded store), the null-value reset, and
the env-pinned-write response shape -- the one behaviour unique to this
route rather than merely a passthrough of the handler already tested in
``tests/unit/mutate/test_settings_update_handler.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import SCOPE_ADMIN, SCOPE_READ, generate_api_key
from trellis.core.settings_registry import SETTINGS_REGISTRY, SettingSpec
from trellis.mutate.policy_source import POLICY_FILENAME
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.settings_store import SettingsStore, resolve_settings_path
from trellis_api.app import create_app
from trellis_api.routes.settings import _parse_error_reason

PACK_HOLDOUT_RATE_ENV = "TRELLIS_PACK_HOLDOUT_RATE"
MEMORY_EXTRACTION_ENV = "TRELLIS_ENABLE_MEMORY_EXTRACTION"
GRAPH_SEEDING_ENV = "TRELLIS_GRAPH_SEEDING"
GRAPH_MAX_SUBGRAPH_DEPTH_ENV = "TRELLIS_GRAPH_MAX_SUBGRAPH_DEPTH"


@pytest.fixture
def registry(tmp_path: Path):
    """Fresh registry bound to the app module for each test."""
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    """In-memory app over the fixture registry.

    Not context-managed -- see the module docstring.
    """
    app = create_app()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_AUTH_MODE", raising=False)
    for env_var in (PACK_HOLDOUT_RATE_ENV, MEMORY_EXTRACTION_ENV, GRAPH_SEEDING_ENV):
        monkeypatch.delenv(env_var, raising=False)


def _mint(registry: StoreRegistry, scopes: list[str], name: str = "test-key") -> str:
    token, record = generate_api_key(name, scopes)
    registry.operational.api_key_store.create(record)
    return token


def _mint_with_key_id(
    registry: StoreRegistry, scopes: list[str], name: str = "test-key"
) -> tuple[str, str]:
    """Like :func:`_mint`, but also returns the minted ``key_id``.

    Needed wherever a test must predict ``changed_by`` (``f"api:settings:
    {key_id}"``), which ``_mint`` alone discards.
    """
    token, record = generate_api_key(name, scopes)
    registry.operational.api_key_store.create(record)
    return token, record.key_id


def _store(registry: StoreRegistry) -> SettingsStore:
    path = resolve_settings_path(registry.stores_dir)
    assert path is not None
    return SettingsStore(path)


def _entry(body: dict, name: str) -> dict:
    (match,) = (row for row in body["settings"] if row["name"] == name)
    return match


# ---------------------------------------------------------------------------
# Auth: admin scope only
# ---------------------------------------------------------------------------


class TestAuth:
    @pytest.fixture(autouse=True)
    def _required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")

    def test_get_missing_credential_is_401(self, client: TestClient) -> None:
        resp = client.get("/api/v1/settings")
        assert resp.status_code == 401

    def test_get_read_scope_denied_403(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        token = _mint(registry, [SCOPE_READ])
        resp = client.get("/api/v1/settings", headers={"X-API-Key": token})
        assert resp.status_code == 403

    def test_put_read_scope_denied_403(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        token = _mint(registry, [SCOPE_READ])
        resp = client.put(
            "/api/v1/settings/pack_holdout_rate",
            json={"value": 0.1},
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 403

    def test_admin_key_passes_on_get(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        token = _mint(registry, [SCOPE_ADMIN])
        resp = client.get("/api/v1/settings", headers={"X-API-Key": token})
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# GET /settings
# ---------------------------------------------------------------------------


class TestListSettings:
    def test_returns_every_registry_entry_with_full_shape(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/v1/settings")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["settings"]) == len(SETTINGS_REGISTRY)
        row = _entry(body, "pack_holdout_rate")
        assert row == {
            "name": "pack_holdout_rate",
            "label": row["label"],  # non-empty, checked below
            "type": "float",
            "default": 0.0,
            "minimum": 0.0,
            "maximum": 1.0,
            "minimum_exclusive": False,
            "enum": None,
            "description": row["description"],  # non-empty, checked below
            "risk_note": row["risk_note"],  # non-empty, checked below (risk=high)
            "surfaces": list(row["surfaces"]),
            "restart_required": False,
            "risk": "high",
            "group": "retrieval",
            "settings_live": True,
            "env_var": PACK_HOLDOUT_RATE_ENV,
            "effective_value": 0.0,
            "overridden_by": "default",
            "stored_value": None,
            "parse_error": None,
            "updated_at": None,
            "settings_writable": True,
        }
        assert row["label"]
        assert row["description"]
        assert row["risk_note"]

    def test_a_malformed_stored_row_is_flagged_not_silently_swallowed(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """Re-gate #867 B4: a stored value the registry's own bounds check
        would refuse (here, 1.5 for a 0..1 rate) makes the resolver fall
        back to the default -- correct -- but ``overridden_by`` still
        reads "settings" off the row's mere presence, and the only way to
        notice the fallback was to diff ``effective_value`` against
        ``stored_value`` by hand. ``stored_value_invalid`` makes that an
        explicit, visible fact instead. Renamed to ``parse_error`` (a fixed,
        value-free reason string, never a bool) so the raw out-of-range
        value can never leak back out through this field either.

        The reason text itself states only that validation rejects the
        row, not what happens next -- some knobs this field covers do not
        fall back to a default at all (an env-pinned knob, or
        ``WriteBehaviourConfig``'s own looser bool/unbounded-int
        resolvers), so the text cannot claim that in general; this case
        happens to fall back to the default too, which the
        ``effective_value`` assertion above already shows.
        """
        settings_path = resolve_settings_path(registry.stores_dir)
        assert settings_path is not None
        SettingsStore(settings_path).set("pack_holdout_rate", 1.5)

        resp = client.get("/api/v1/settings")
        assert resp.status_code == 200, resp.text
        row = _entry(resp.json(), "pack_holdout_rate")

        assert row["stored_value"] == 1.5
        assert row["effective_value"] == 0.0  # the module default
        assert row["parse_error"] == "stored value is not a finite number in range"
        assert "1.5" not in row["parse_error"]

    def test_a_valid_stored_row_is_not_flagged(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        settings_path = resolve_settings_path(registry.stores_dir)
        assert settings_path is not None
        SettingsStore(settings_path).set("pack_holdout_rate", 0.2)

        resp = client.get("/api/v1/settings")
        row = _entry(resp.json(), "pack_holdout_rate")

        assert row["effective_value"] == 0.2
        assert row["parse_error"] is None

    def test_a_malformed_stored_int_row_gets_its_own_reason(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """f867 follow-up SF1: an int-typed knob with a stored float such
        as 1.5 previously read the same ``"stored value is not a finite
        number in range"`` text the float case uses above, which
        describes the wrong coercion. The int-typed branch of
        ``_parse_error_reason`` now has its own fixed, value-free text.
        """
        settings_path = resolve_settings_path(registry.stores_dir)
        assert settings_path is not None
        SettingsStore(settings_path).set("noise_demotion_min_attributed_packs", 1.5)

        resp = client.get("/api/v1/settings")
        assert resp.status_code == 200, resp.text
        row = _entry(resp.json(), "noise_demotion_min_attributed_packs")

        assert row["stored_value"] == 1.5
        assert row["effective_value"] == 5  # the module default
        assert row["parse_error"] == "stored value is not an integer in range"
        assert row["parse_error"] != "stored value is not a finite number in range"
        assert "1.5" not in row["parse_error"]

    def test_a_malformed_stored_bool_row_is_flagged(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """f867 follow-up S1a: a stored value that fails the bool
        coercion (neither a recognized true/false token) gets the
        bool-specific fixed reason, never the stored string itself.
        """
        settings_path = resolve_settings_path(registry.stores_dir)
        assert settings_path is not None
        SettingsStore(settings_path).set("memory_extraction", "maybe")

        resp = client.get("/api/v1/settings")
        assert resp.status_code == 200, resp.text
        row = _entry(resp.json(), "memory_extraction")

        assert row["stored_value"] == "maybe"
        assert row["parse_error"] == "stored value is not a recognized boolean"
        assert "maybe" not in row["parse_error"]

    def test_group_labels_cover_every_entrys_group(self, client: TestClient) -> None:
        """brief e179 step 8: the UI's filter box reads display labels from
        ``groups``, not a hardcoded string -- so every group a real entry
        names must have a label.
        """
        resp = client.get("/api/v1/settings")
        body = resp.json()
        used_groups = {row["group"] for row in body["settings"]}
        assert used_groups <= set(body["groups"])
        assert all(
            isinstance(label, str) and label for label in body["groups"].values()
        )

    def test_minimum_exclusive_is_reported_for_the_one_entry_that_sets_it(
        self, client: TestClient
    ) -> None:
        """``reconcile_timeout_s`` refuses 0 (its own ``minimum``) because
        its env reader degrades a zero timeout to the shipped default
        rather than honouring it -- the UI must say "> 0", not "min 0",
        or an operator reads 0 as an accepted value it silently is not.
        """
        resp = client.get("/api/v1/settings")
        row = _entry(resp.json(), "reconcile_timeout_s")
        assert row["minimum"] == 0.0
        assert row["minimum_exclusive"] is True

        # And the common case must stay the inclusive default -- this is
        # what distinguishes "reported" from "always true".
        other = _entry(resp.json(), "pack_holdout_rate")
        assert other["minimum_exclusive"] is False

    def test_stored_value_and_updated_at_reflect_a_real_override(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """Unset -> both None (proven above); once a value is written, the
        GET response's stored_value/updated_at must show it, not just
        effective_value -- the two can legitimately differ (env-pinned).
        """
        path = resolve_settings_path(registry.stores_dir)
        assert path is not None
        store = SettingsStore(path)
        store.set("pack_holdout_rate", 0.2)

        resp = client.get("/api/v1/settings")
        row = _entry(resp.json(), "pack_holdout_rate")
        assert row["stored_value"] == 0.2
        assert row["updated_at"] is not None

    def test_store_degradation_is_none_when_healthy(self, client: TestClient) -> None:
        resp = client.get("/api/v1/settings")
        assert resp.json()["store_degradation"] is None

    def test_store_degradation_reported_when_file_is_corrupt(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        path = resolve_settings_path(registry.stores_dir)
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")

        resp = client.get("/api/v1/settings")
        assert resp.status_code == 200, resp.text
        assert resp.json()["store_degradation"] is not None


# ---------------------------------------------------------------------------
# PUT/POST /settings/{name} — success and refusals
# ---------------------------------------------------------------------------


class TestUpdateSetting:
    def test_put_success_returns_command_response_and_persists(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.put("/api/v1/settings/pack_holdout_rate", json={"value": 0.3})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "success"
        assert body["created_id"] == "pack_holdout_rate"
        assert _store(registry).get("pack_holdout_rate").value == 0.3

        follow_up = client.get("/api/v1/settings").json()
        row = _entry(follow_up, "pack_holdout_rate")
        assert row["effective_value"] == 0.3
        assert row["overridden_by"] == "settings"

    def test_post_is_equivalent_to_put(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        resp = client.post("/api/v1/settings/pack_holdout_rate", json={"value": 0.25})
        assert resp.status_code == 200, resp.text
        assert _store(registry).get("pack_holdout_rate").value == 0.25

    def test_changed_by_is_the_authenticated_principal_not_requested_by(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """M9 (re-gate #857): ``changed_by`` must come from ``ctx``, never
        from the client-controlled ``req.requested_by`` body field.

        A client could otherwise write ``requested_by: "someone-else"``
        and have the audit trail record the write as made by whoever it
        names. The route's docstring says ``changed_by`` is
        ``f"api:settings:{principal}"`` where ``principal`` is the
        authenticated key id -- this proves it on the live route, not
        just at the handler layer (``test_settings_update_handler.py``'s
        ``test_requested_by_claim_is_carried_on_the_event_when_present``
        constructs the ``Command`` directly and so cannot catch a route
        that wires the wrong field into it). Auth mode must be forced to
        ``required`` -- the fixture default (``off``) ignores credentials
        entirely and would leave ``ctx.key_id`` ``None`` regardless.
        """
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token, key_id = _mint_with_key_id(registry, [SCOPE_ADMIN])
        resp = client.put(
            "/api/v1/settings/pack_holdout_rate",
            json={"value": 0.3, "requested_by": "someone-else"},
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 200, resp.text

        events = registry.operational.event_log.get_events(
            event_type=EventType.SETTINGS_UPDATED,
            order="asc",
            limit=100,
        )
        payload = events[-1].payload
        assert payload["changed_by"] == f"api:settings:{key_id}"
        assert payload["changed_by"] != "someone-else"
        assert payload["changed_by"] != "api:settings:someone-else"
        assert payload["requested_by_claim"] == "someone-else"

    def test_unknown_name_is_400(self, client: TestClient) -> None:
        resp = client.put("/api/v1/settings/not_a_real_setting", json={"value": 1})
        assert resp.status_code == 400
        assert "not_a_real_setting" in resp.json()["detail"]

    def test_unknown_name_still_goes_through_the_governed_path(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """The route must not short-circuit before ``MutationExecutor``.

        An earlier draft of this route pre-checked the name and raised
        an ``HTTPException`` before building the ``Command`` at all --
        which skipped Stage 2 and left no audit trail, unlike every
        other refusal reason. This asserts the real governed path ran:
        a ``MUTATION_REJECTED`` event lands for the attempt (the handler
        raises ``ValidationError(code="unknown_setting")``, and
        ``executor._emit_rejection`` is the one path every rejection
        reason -- unknown name, wrong type, out of range, degraded
        store -- emits through; there is no separate ``WRITE_REJECTED``
        event type for this case, see ``executor.py``).
        """
        resp = client.put("/api/v1/settings/not_a_real_setting", json={"value": 1})
        assert resp.status_code == 400

        events = registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_REJECTED, entity_id="not_a_real_setting"
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["reason"] == "unknown_setting"
        assert "not_a_real_setting" in payload["message"]

    def test_wrong_type_is_400(self, client: TestClient) -> None:
        resp = client.put(
            "/api/v1/settings/memory_extraction", json={"value": "not-a-bool"}
        )
        assert resp.status_code == 400
        assert "boolean" in resp.json()["detail"]

    def test_out_of_range_is_400(self, client: TestClient) -> None:
        resp = client.put("/api/v1/settings/pack_holdout_rate", json={"value": 5.0})
        assert resp.status_code == 400

    def test_missing_value_field_is_422(self, client: TestClient) -> None:
        resp = client.put("/api/v1/settings/pack_holdout_rate", json={})
        assert resp.status_code == 422

    def test_entity_type_setting_deny_policy_refuses_the_route(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """Required item 1's third check: a deny policy scoped to
        ``entity_type=setting`` (the scope ``trellis policy add
        --entity-type setting`` produces) blocks this route too -- not
        just an operation-name or wildcard rule. Stage 2 always runs
        through :func:`build_settings_executor`
        (``trellis_api.routes.settings._execute_command``), so this is
        the same gate every other curate operation gets.
        """
        policy = Policy(
            policy_type=PolicyType.MUTATION,
            scope=PolicyScope(level="entity_type", value="setting"),
            rules=[PolicyRule(operation="settings.update", action="deny")],
            enforcement=Enforcement.ENFORCE,
        )
        assert registry.stores_dir is not None
        registry.stores_dir.mkdir(parents=True, exist_ok=True)
        (registry.stores_dir / POLICY_FILENAME).write_text(
            json.dumps({"policies": [policy.model_dump(mode="json")]}),
            encoding="utf-8",
        )

        resp = client.put("/api/v1/settings/pack_holdout_rate", json={"value": 0.9})

        assert resp.status_code == 400, resp.text
        assert _store(registry).get("pack_holdout_rate") is None

    def test_null_value_resets_the_override(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        set_resp = client.put("/api/v1/settings/pack_holdout_rate", json={"value": 0.4})
        assert set_resp.status_code == 200, set_resp.text

        reset_resp = client.put(
            "/api/v1/settings/pack_holdout_rate", json={"value": None}
        )
        assert reset_resp.status_code == 200, reset_resp.text
        assert _store(registry).get("pack_holdout_rate") is None

        follow_up = client.get("/api/v1/settings").json()
        row = _entry(follow_up, "pack_holdout_rate")
        assert row["effective_value"] == 0.0
        assert row["overridden_by"] == "default"

    def test_degraded_store_refuses_the_write(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """409, not 400 (F1): a degraded store is not a bad request.

        The store file on disk is unharmed and the fix is an operator
        action (``trellis admin settings list``/inspect the file), not a
        different request body -- so this is shaped like
        ``routes/policies.py``'s precedent for the same error class.

        The recovery command is asserted from its own ``detail["recovery"]``
        field, not out of ``detail["message"]`` -- that string goes through
        ``sanitize_error_message``'s 512-char cap (re-gate #857 item 2),
        which can truncate a long ``stores_dir`` path's ``mv ... .corrupt``
        command away. ``store_degradation`` must be the real object too,
        not the hard-coded ``None`` the pre-fix branch returned.
        """
        path = resolve_settings_path(registry.stores_dir)
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")

        resp = client.put("/api/v1/settings/pack_holdout_rate", json={"value": 0.1})
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "degraded_store_write"
        assert "degrad" in detail["message"].lower()
        assert detail["recovery"].startswith("mv ")
        assert detail["recovery"].endswith(".corrupt")
        assert str(path) in detail["recovery"]
        assert detail["store_degradation"] is not None
        assert detail["store_degradation"]["path"] == str(path)
        assert detail["store_degradation"]["recovery"] == detail["recovery"]


# ---------------------------------------------------------------------------
# Env-pinned write: still stores, but reports the env value as effective
# ---------------------------------------------------------------------------


class TestEnvPinnedWrite:
    def test_env_pinned_write_returns_accepted_shape_and_still_stores(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(PACK_HOLDOUT_RATE_ENV, "0.9")

        resp = client.put("/api/v1/settings/pack_holdout_rate", json={"value": 0.3})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["accepted"] is True
        assert body["name"] == "pack_holdout_rate"
        assert body["stored_value"] == 0.3
        assert body["effective_value"] == 0.9
        assert body["overridden_by"] == "env"
        assert PACK_HOLDOUT_RATE_ENV in body["note"]

        # The write still landed -- ready the moment the env var is unset.
        assert _store(registry).get("pack_holdout_rate").value == 0.3

    def test_env_pinned_write_reports_the_stored_value_not_the_raw_request(
        self,
        client: TestClient,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """M13 (re-gate #857): ``stored_value`` must echo what the handler
        actually parsed and stored, never ``req.value`` verbatim.

        The existing env-pinned test above sends a float (``0.3``), which
        can't distinguish the two -- a float round-trips identically
        either way. Sending the numeric string ``"0.10"`` can: the stored
        (and coerced) value is the float ``0.1``, while the raw request
        value is the string ``"0.10"``. A mutant that swaps
        ``stored_value`` for ``req.value`` passes the float-sending test
        but fails this one on both type and value.
        """
        monkeypatch.setenv(PACK_HOLDOUT_RATE_ENV, "0.9")

        resp = client.put("/api/v1/settings/pack_holdout_rate", json={"value": "0.10"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["stored_value"] == 0.1
        assert isinstance(body["stored_value"], float)
        assert _store(registry).get("pack_holdout_rate").value == 0.1

    def test_env_pinned_write_not_shaped_as_a_command_response(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PACK_HOLDOUT_RATE_ENV, "0.9")
        resp = client.put("/api/v1/settings/pack_holdout_rate", json={"value": 0.3})
        body = resp.json()
        # Distinct from the plain CommandResponse shape: no "operation"/"message".
        assert "operation" not in body
        assert "created_id" not in body


# ---------------------------------------------------------------------------
# graph_seeding's asymmetric env semantics, reused rather than reimplemented
# ---------------------------------------------------------------------------


#: ``(setting_name, env_var, default, env_value, parsed_env, settings_value)``
#: for every registry entry with a dedicated resolver. These crashed
#: ``GET /settings`` outright until ``_effective_values`` called each real
#: resolver -- ``effective[spec.name]`` raised ``KeyError`` for all eight,
#: because the function only ever populated the twelve ``WriteBehaviourConfig``
#: fields plus the two hand-cased booleans above it. Covering every entry
#: here (not just one as a representative) is deliberate: the bug was
#: per-entry, so proof has to be too.
_RESOLVER_BACKED_KNOBS = (
    (
        "noise_demotion_min_attributed_packs",
        "TRELLIS_NOISE_DEMOTION_MIN_ATTRIBUTED_PACKS",
        5,
        "9",
        9,
        7,
    ),
    (
        "noise_demotion_min_unhelpful_citations",
        "TRELLIS_NOISE_DEMOTION_MIN_UNHELPFUL_CITATIONS",
        2,
        "4",
        4,
        3,
    ),
    ("advisory_min_sample_size", "TRELLIS_ADVISORY_MIN_SAMPLE_SIZE", 5, "8", 8, 6),
    (
        "advisory_min_effect_size",
        "TRELLIS_ADVISORY_MIN_EFFECT_SIZE",
        0.15,
        "0.3",
        0.3,
        0.25,
    ),
    ("capture_warn_threshold", "TRELLIS_CAPTURE_WARN_THRESHOLD", 3, "7", 7, 1),
    (
        "capture_warn_window_hours",
        "TRELLIS_CAPTURE_WARN_WINDOW_HOURS",
        24,
        "48",
        48,
        12,
    ),
    # graph_max_subgraph_depth is deliberately absent from this list (see
    # TestGraphMaxSubgraphDepthIsEnvOnly below): unlike every other entry
    # here, its settings-store override is never consulted by anything,
    # so "a stored override is previewed" is false for this one knob --
    # that was the B3 bug, not a case this generic table should still
    # assert.
)


class TestResolverBackedEffectiveValues:
    """Each entry's ``effective_value`` must equal its real resolver's
    output, not a generic reimplementation -- proven by actually setting
    the env var / settings-store override and reading the resolved value
    back, the same way ``TestGraphSeedingEffectiveValue`` below proves it
    for the two hand-cased booleans.
    """

    @pytest.mark.parametrize(
        ("name", "env_var", "default", "env_value", "parsed_env", "settings_value"),
        _RESOLVER_BACKED_KNOBS,
    )
    def test_defaults_when_nothing_is_set(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        env_var: str,
        default: float,
        env_value: str,
        parsed_env: float,
        settings_value: float,
    ) -> None:
        monkeypatch.delenv(env_var, raising=False)
        body = client.get("/api/v1/settings").json()
        row = _entry(body, name)
        assert row["effective_value"] == default
        assert row["overridden_by"] == "default"

    @pytest.mark.parametrize(
        ("name", "env_var", "default", "env_value", "parsed_env", "settings_value"),
        _RESOLVER_BACKED_KNOBS,
    )
    def test_env_overrides_the_default(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        env_var: str,
        default: float,
        env_value: str,
        parsed_env: float,
        settings_value: float,
    ) -> None:
        monkeypatch.setenv(env_var, env_value)
        body = client.get("/api/v1/settings").json()
        row = _entry(body, name)
        assert row["effective_value"] == parsed_env
        assert row["overridden_by"] == "env"

    @pytest.mark.parametrize(
        ("name", "env_var", "default", "env_value", "parsed_env", "settings_value"),
        _RESOLVER_BACKED_KNOBS,
    )
    def test_a_stored_override_is_previewed_when_env_is_unset(
        self,
        client: TestClient,
        registry: StoreRegistry,
        name: str,
        env_var: str,
        default: float,
        env_value: str,
        parsed_env: float,
        settings_value: float,
    ) -> None:
        _store(registry).set(name, settings_value)
        body = client.get("/api/v1/settings").json()
        row = _entry(body, name)
        assert row["effective_value"] == settings_value
        assert row["overridden_by"] == "settings"


class TestGraphMaxSubgraphDepthIsEnvOnly:
    """B3 (re-gate #867): nothing ever reads a stored override for this
    knob -- not ``trellis.stores.base.graph``'s three backends, which all
    resolve :data:`MAX_SUBGRAPH_DEPTH` once at import from the
    environment only, and (unlike ``minhash_seed_max_docs``) not even
    after a restart. ``settings_writable=False`` makes the write path
    refuse rather than accept a value that would silently never matter.
    """

    def test_default_when_nothing_is_set(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(GRAPH_MAX_SUBGRAPH_DEPTH_ENV, raising=False)
        body = client.get("/api/v1/settings").json()
        row = _entry(body, "graph_max_subgraph_depth")
        assert row["effective_value"] == 6
        assert row["overridden_by"] == "default"
        assert row["settings_writable"] is False

    def test_env_still_overrides_the_default(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GRAPH_MAX_SUBGRAPH_DEPTH_ENV, "10")
        body = client.get("/api/v1/settings").json()
        row = _entry(body, "graph_max_subgraph_depth")
        assert row["effective_value"] == 10
        assert row["overridden_by"] == "env"

    def test_a_stored_value_is_never_previewed_as_effective(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        """This is the exact B3 bug: a stray stored row used to be echoed
        back as ``effective_value`` even though no code path ever reads
        it. ``overridden_by`` must agree with ``effective_value`` here --
        reporting "settings" while serving the default would be its own,
        subtler version of the same lie.
        """
        _store(registry).set("graph_max_subgraph_depth", 8)
        body = client.get("/api/v1/settings").json()
        row = _entry(body, "graph_max_subgraph_depth")
        assert row["effective_value"] == 6
        assert row["overridden_by"] == "default"
        assert row["stored_value"] == 8  # still reported, just inert

    def test_put_a_value_is_refused(self, client: TestClient) -> None:
        resp = client.put(
            "/api/v1/settings/graph_max_subgraph_depth", json={"value": 10}
        )
        assert resp.status_code == 400, resp.text
        assert "TRELLIS_GRAPH_MAX_SUBGRAPH_DEPTH" in resp.text

    def test_clearing_a_stray_stored_value_is_still_allowed(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        _store(registry).set("graph_max_subgraph_depth", 8)
        resp = client.put(
            "/api/v1/settings/graph_max_subgraph_depth", json={"value": None}
        )
        assert resp.status_code == 200, resp.text
        assert _store(registry).get("graph_max_subgraph_depth") is None


class TestCaptureStrictEffectiveValue:
    """``capture_strict`` is boolean and shares the opt-out-token
    semantics ``strict_mode`` already applies to a sweep -- 0/false/no/off
    disable it, everything else (including an unrecognised token) leaves
    it strict, same asymmetric contract as ``graph_seeding``.
    """

    def test_defaults_to_strict(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRELLIS_CAPTURE_STRICT", raising=False)
        row = _entry(client.get("/api/v1/settings").json(), "capture_strict")
        assert row["effective_value"] is True
        assert row["overridden_by"] == "default"

    def test_env_opt_out_disables_it(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_CAPTURE_STRICT", "0")
        row = _entry(client.get("/api/v1/settings").json(), "capture_strict")
        assert row["effective_value"] is False
        assert row["overridden_by"] == "env"

    def test_stored_false_override_is_honoured(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        _store(registry).set("capture_strict", False)
        row = _entry(client.get("/api/v1/settings").json(), "capture_strict")
        assert row["effective_value"] is False
        assert row["overridden_by"] == "settings"


class TestGraphSeedingEffectiveValue:
    def test_unset_env_defaults_on(self, client: TestClient) -> None:
        body = client.get("/api/v1/settings").json()
        row = _entry(body, "graph_seeding")
        assert row["effective_value"] is True
        assert row["overridden_by"] == "default"

    def test_unrecognised_env_token_is_still_on(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GRAPH_SEEDING_ENV, "banana")
        body = client.get("/api/v1/settings").json()
        row = _entry(body, "graph_seeding")
        assert row["effective_value"] is True
        assert row["overridden_by"] == "env"

    def test_explicit_falsey_token_turns_it_off(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GRAPH_SEEDING_ENV, "false")
        body = client.get("/api/v1/settings").json()
        row = _entry(body, "graph_seeding")
        assert row["effective_value"] is False
        assert row["overridden_by"] == "env"


class TestParseErrorReasonIsFixedPerType:
    """f867 follow-up SF1/S1a/S1c, unit-level on ``_parse_error_reason``.

    No shipped :data:`SETTINGS_REGISTRY` entry sets ``enum`` today (grep
    confirms it), so the enum branch is otherwise dead in every HTTP-level
    test above. Exercising the private function directly with a synthetic
    ``SettingSpec`` is the only way to pin that branch's text without
    adding a throwaway production setting just to carry a test.
    """

    def test_int_reason_is_distinct_from_float_reason(self) -> None:
        int_spec = SettingSpec(
            name="probe_int",
            label="Probe int",
            type="int",
            default=0,
            description="d",
            surfaces=("api",),
            restart_required=False,
            risk="low",
            group="ops",
            settings_live=True,
            env_var="TRELLIS_PROBE_INT",
        )
        float_spec = SettingSpec(
            name="probe_float",
            label="Probe float",
            type="float",
            default=0.0,
            description="d",
            surfaces=("api",),
            restart_required=False,
            risk="low",
            group="ops",
            settings_live=True,
            env_var="TRELLIS_PROBE_FLOAT",
        )
        int_reason = _parse_error_reason(int_spec)
        float_reason = _parse_error_reason(float_spec)
        assert int_reason == "stored value is not an integer in range"
        assert float_reason == "stored value is not a finite number in range"
        assert int_reason != float_reason

    def test_enum_reason(self) -> None:
        enum_spec = SettingSpec(
            name="probe_enum",
            label="Probe enum",
            type="str",
            default="a",
            description="d",
            surfaces=("api",),
            restart_required=False,
            risk="low",
            group="ops",
            settings_live=True,
            env_var="TRELLIS_PROBE_ENUM",
            enum=("a", "b"),
        )
        assert (
            _parse_error_reason(enum_spec)
            == "stored value is not one of the allowed options"
        )
