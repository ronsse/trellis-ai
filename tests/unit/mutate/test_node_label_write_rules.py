"""Write rules for a ``data_classification`` label held in a node's properties.

A node label lives only as the raw ``data_classification`` key in the node's
property bag. Two rules hold for every graph writer that replaces or merges
that bag (``entity.create``, ``entity.update``, ``observation.record``,
``measurement.record``):

* **Never lowered.** A write that supplies a weaker label, or none, keeps the
  stricter stored record; an equal or stricter supplied record wins.
* **Never above the writer.** ``entity.create`` and ``entity.update`` refuse a
  supplied label above the executor's clearance, and nothing is written.

A bag that never carried the key gets none: the rules are transparent to an
unlabelled store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from trellis.mutate import build_curate_executor
from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandBatch,
    CommandResult,
    CommandStatus,
    Operation,
)
from trellis.schemas.classification import DataClassification
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from trellis.schemas.classification import Sensitivity

NODE_ID = "dataset:test:main.sales.orders"


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[StoreRegistry]:
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    yield reg
    reg.close()


def _record(level: str) -> dict[str, Any]:
    return DataClassification(
        sensitivity=level,  # type: ignore[arg-type]
        classified_by=["source-inherit"],
    ).model_dump(mode="json")


def _create(
    properties: dict[str, Any] | None = None, *, if_absent: bool = False
) -> Command:
    args: dict[str, Any] = {
        "entity_id": NODE_ID,
        "entity_type": "dataset",
        "name": "orders",
        "properties": properties or {},
    }
    if if_absent:
        args["if_absent"] = True
    return Command(operation=Operation.ENTITY_CREATE, args=args, requested_by="tester")


def _update(properties: dict[str, Any]) -> Command:
    return Command(
        operation=Operation.ENTITY_UPDATE,
        args={"entity_id": NODE_ID, "properties": properties},
        requested_by="tester",
    )


def _run(registry: StoreRegistry, command: Command, clearance: Sensitivity) -> Any:
    return build_curate_executor(registry, clearance=clearance).execute(command)


def _stored(registry: StoreRegistry, node_id: str = NODE_ID) -> dict[str, Any]:
    node = registry.knowledge.graph_store.get_node(node_id)
    assert node is not None
    return dict(node["properties"])


def _version_count(registry: StoreRegistry, node_id: str = NODE_ID) -> int:
    return len(registry.knowledge.graph_store.get_node_history(node_id))


def _ok(result: Any) -> None:
    assert result.status == CommandStatus.SUCCESS, result.message


def test_entity_create_over_a_labelled_node_keeps_the_stored_label(
    registry: StoreRegistry,
) -> None:
    _ok(
        _run(
            registry,
            _create({"data_classification": _record("restricted")}),
            "restricted",
        )
    )
    _ok(_run(registry, _create({"owner": "sales"}), "restricted"))
    props = _stored(registry)
    assert props["data_classification"]["sensitivity"] == "restricted"
    assert props["owner"] == "sales"


class TestEntityCreate:
    def test_a_stricter_supplied_label_replaces_a_weaker_stored_one(
        self, registry: StoreRegistry
    ) -> None:
        _run(
            registry,
            _create({"data_classification": _record("confidential")}),
            "restricted",
        )
        _ok(
            _run(
                registry,
                _create({"data_classification": _record("restricted")}),
                "restricted",
            )
        )
        assert _stored(registry)["data_classification"]["sensitivity"] == "restricted"

    def test_an_equal_rank_supplied_record_replaces_the_stored_one(
        self, registry: StoreRegistry
    ) -> None:
        _run(
            registry,
            _create({"data_classification": _record("confidential")}),
            "restricted",
        )
        supplied = {"sensitivity": "confidential", "classified_by": ["operator"]}
        _ok(
            _run(
                registry,
                _create({"data_classification": supplied}),
                "restricted",
            )
        )
        assert _stored(registry)["data_classification"] == supplied

    def test_a_weaker_supplied_label_keeps_the_stored_one(
        self, registry: StoreRegistry
    ) -> None:
        _run(
            registry,
            _create({"data_classification": _record("restricted")}),
            "restricted",
        )
        _ok(
            _run(
                registry,
                _create({"data_classification": _record("confidential")}),
                "restricted",
            )
        )
        assert _stored(registry)["data_classification"]["sensitivity"] == "restricted"

    def test_a_stored_malformed_record_is_carried_unchanged(
        self, registry: StoreRegistry
    ) -> None:
        malformed = {"sensitivity": "top-secret"}
        _run(registry, _create({"data_classification": malformed}), "restricted")
        _ok(_run(registry, _create({"owner": "sales"}), "restricted"))
        assert _stored(registry)["data_classification"] == malformed

    def test_a_label_above_the_writer_is_refused_and_nothing_is_written(
        self, registry: StoreRegistry
    ) -> None:
        result = _run(
            registry,
            _create({"data_classification": _record("restricted")}),
            "internal",
        )
        assert result.status == CommandStatus.REJECTED
        assert NODE_ID in result.message
        assert "restricted" not in result.message
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None

    def test_a_label_at_the_writer_clearance_is_written(
        self, registry: StoreRegistry
    ) -> None:
        _ok(
            _run(
                registry,
                _create({"data_classification": _record("internal")}),
                "internal",
            )
        )
        assert _stored(registry)["data_classification"]["sensitivity"] == "internal"

    def test_an_if_absent_create_over_a_labelled_node_writes_nothing(
        self, registry: StoreRegistry
    ) -> None:
        _run(
            registry,
            _create({"data_classification": _record("restricted")}),
            "restricted",
        )
        before = _version_count(registry)
        result = _run(registry, _create({"owner": "x"}, if_absent=True), "restricted")
        assert result.status == CommandStatus.SUCCESS
        assert _version_count(registry) == before
        props = _stored(registry)
        assert props["data_classification"]["sensitivity"] == "restricted"
        assert "owner" not in props

    def test_an_unlabelled_node_stays_unlabelled_across_a_recreate(
        self, registry: StoreRegistry
    ) -> None:
        _ok(_run(registry, _create({"owner": "sales"}), "restricted"))
        _ok(_run(registry, _create({"owner": "finance"}), "restricted"))
        props = _stored(registry)
        assert "data_classification" not in props
        assert props["owner"] == "finance"


class TestEntityUpdate:
    def test_a_weaker_supplied_label_keeps_the_stored_one(
        self, registry: StoreRegistry
    ) -> None:
        _run(
            registry,
            _create({"data_classification": _record("restricted")}),
            "restricted",
        )
        _ok(
            _run(
                registry,
                _update({"data_classification": _record("confidential")}),
                "restricted",
            )
        )
        assert _stored(registry)["data_classification"]["sensitivity"] == "restricted"

    def test_a_stricter_supplied_label_replaces_a_weaker_stored_one(
        self, registry: StoreRegistry
    ) -> None:
        _run(
            registry,
            _create({"data_classification": _record("confidential")}),
            "restricted",
        )
        _ok(
            _run(
                registry,
                _update({"data_classification": _record("restricted")}),
                "restricted",
            )
        )
        assert _stored(registry)["data_classification"]["sensitivity"] == "restricted"

    def test_a_label_above_the_writer_is_refused_and_nothing_is_written(
        self, registry: StoreRegistry
    ) -> None:
        _run(registry, _create({"owner": "sales"}), "restricted")
        before = _version_count(registry)
        result = _run(
            registry,
            _update({"data_classification": _record("restricted")}),
            "internal",
        )
        assert result.status == CommandStatus.REJECTED
        assert NODE_ID in result.message
        assert _version_count(registry) == before
        assert "data_classification" not in _stored(registry)


def _observation(observation_id: str, subject: str) -> dict[str, Any]:
    return {
        "observation_id": observation_id,
        "subject_entity_id": subject,
        "subject_entity_type": "dataset",
        "observer_agent_id": "tester",
        "content": "row count fell",
        "confidence": 0.5,
    }


def _measurement(measurement_id: str, subject: str) -> dict[str, Any]:
    return {
        "measurement_id": measurement_id,
        "subject_entity_id": subject,
        "subject_entity_type": "dataset",
        "metric_name": "rows",
        "metric_value": 3.0,
        "observer_agent_id": "tester",
    }


def _label_node_directly(registry: StoreRegistry, node_id: str) -> None:
    """Give an existing record node a stored label, as a raw store write."""
    node = registry.knowledge.graph_store.get_node(node_id)
    assert node is not None
    props = dict(node["properties"])
    props["data_classification"] = _record("restricted")
    registry.knowledge.graph_store.upsert_node(node_id, node["node_type"], props)


class TestRecordVerbs:
    def test_observation_rerecord_keeps_the_stored_label(
        self, registry: StoreRegistry
    ) -> None:
        obs = _observation("obs-1", NODE_ID)
        command = Command(
            operation=Operation.OBSERVATION_RECORD,
            args={"observation": obs},
            requested_by="tester",
        )
        _ok(_run(registry, command, "restricted"))
        _label_node_directly(registry, "obs-1")
        _ok(_run(registry, command, "restricted"))
        props = _stored(registry, "obs-1")
        assert props["data_classification"]["sensitivity"] == "restricted"

    def test_measurement_rerecord_keeps_the_stored_label(
        self, registry: StoreRegistry
    ) -> None:
        meas = _measurement("meas-1", NODE_ID)
        command = Command(
            operation=Operation.MEASUREMENT_RECORD,
            args={"measurement": meas},
            requested_by="tester",
        )
        _ok(_run(registry, command, "restricted"))
        _label_node_directly(registry, "meas-1")
        _ok(_run(registry, command, "restricted"))
        props = _stored(registry, "meas-1")
        assert props["data_classification"]["sensitivity"] == "restricted"


# --- #1265: the winning record keeps the other record's tags and jurisdiction ---
#
# The document path (``_relabelled_record``) carries a prior record's
# ``regulatory_tags`` and ``jurisdiction`` across a relabel, and refuses a write
# that would discard them. A node label now does the same: whichever readable
# record wins on sensitivity carries the union of both records' tags (stored
# first) and, when it names no jurisdiction, the other record's.


def _labelled(level: str, **fields: Any) -> dict[str, Any]:
    return {"sensitivity": level, **fields}


def _seed(registry: StoreRegistry, record: object) -> None:
    _ok(_run(registry, _create({"data_classification": record}), "restricted"))


def _route(route: str, record: object) -> Command:
    props = {"data_classification": record}
    return _create(props) if route == "create" else _update(props)


ROUTES = ["create", "update"]


def _events(registry: StoreRegistry, event_type: Any) -> list[Any]:
    return registry.operational.event_log.get_events(
        event_type=event_type, entity_id=None, limit=1000
    )


@pytest.mark.parametrize("route", ROUTES)
class TestWinnerCarriesTags:
    def test_equal_rank_keeps_the_stored_tags(
        self, registry: StoreRegistry, route: str
    ) -> None:
        _seed(registry, _labelled("confidential", regulatory_tags=["phi"]))
        supplied = _labelled("confidential", classified_by=["source-inherit"])
        _ok(_run(registry, _route(route, supplied), "restricted"))
        assert _stored(registry)["data_classification"] == {
            "sensitivity": "confidential",
            "classified_by": ["source-inherit"],
            "regulatory_tags": ["phi"],
        }

    def test_a_stricter_supplied_record_keeps_the_stored_tags(
        self, registry: StoreRegistry, route: str
    ) -> None:
        _seed(registry, _labelled("internal", regulatory_tags=["phi", "pii"]))
        supplied = _labelled("restricted", regulatory_tags=["pii", "gdpr"])
        _ok(_run(registry, _route(route, supplied), "restricted"))
        record = _stored(registry)["data_classification"]
        assert record["sensitivity"] == "restricted"
        assert record["regulatory_tags"] == ["phi", "pii", "gdpr"]

    def test_a_kept_stricter_stored_record_gains_the_supplied_tags(
        self, registry: StoreRegistry, route: str
    ) -> None:
        stored = _labelled("restricted", regulatory_tags=["phi"], classified_by=["a"])
        _seed(registry, stored)
        supplied = _labelled("internal", regulatory_tags=["pii"], classified_by=["b"])
        _ok(_run(registry, _route(route, supplied), "restricted"))
        assert _stored(registry)["data_classification"] == {
            "sensitivity": "restricted",
            "regulatory_tags": ["phi", "pii"],
            "classified_by": ["a"],
        }

    def test_the_winner_keeps_its_own_classified_by(
        self, registry: StoreRegistry, route: str
    ) -> None:
        _seed(registry, _labelled("internal", classified_by=["caller"]))
        supplied = _labelled("internal", classified_by=["source-inherit"])
        _ok(_run(registry, _route(route, supplied), "restricted"))
        assert _stored(registry)["data_classification"] == supplied

    def test_a_winner_without_a_jurisdiction_takes_the_stored_one(
        self, registry: StoreRegistry, route: str
    ) -> None:
        _seed(registry, _labelled("confidential", jurisdiction=["eu"]))
        _ok(_run(registry, _route(route, _labelled("confidential")), "restricted"))
        assert _stored(registry)["data_classification"]["jurisdiction"] == ["eu"]

    def test_a_kept_stored_record_takes_the_supplied_jurisdiction(
        self, registry: StoreRegistry, route: str
    ) -> None:
        _seed(registry, _labelled("restricted"))
        supplied = _labelled("internal", jurisdiction=["us"])
        _ok(_run(registry, _route(route, supplied), "restricted"))
        assert _stored(registry)["data_classification"]["jurisdiction"] == ["us"]

    def test_a_winner_with_its_own_jurisdiction_keeps_it(
        self, registry: StoreRegistry, route: str
    ) -> None:
        # The document path never meets two differing jurisdictions, so the
        # winner's own stands, as before #1265.
        _seed(registry, _labelled("confidential", jurisdiction=["eu"]))
        supplied = _labelled("confidential", jurisdiction=["us"])
        _ok(_run(registry, _route(route, supplied), "restricted"))
        assert _stored(registry)["data_classification"]["jurisdiction"] == ["us"]

    def test_the_success_event_is_unchanged_and_names_no_label(
        self, registry: StoreRegistry, route: str
    ) -> None:
        _seed(registry, _labelled("confidential", regulatory_tags=["phi"]))
        _ok(_run(registry, _route(route, _labelled("confidential")), "restricted"))
        kind = (
            EventType.ENTITY_CREATED if route == "create" else EventType.ENTITY_UPDATED
        )
        last = _events(registry, kind)[-1]
        assert set(last.payload) == {"name", "node_role", "document_ids"}
        executed = _events(registry, EventType.MUTATION_EXECUTED)[-1]
        assert "data_classification" not in str(executed.payload)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("unreadable", [None, {"sensitivity": "top-secret"}, "x"])
class TestUnreadableSuppliedRecord:
    def test_over_a_stored_record_with_tags_it_is_refused(
        self, registry: StoreRegistry, route: str, unreadable: object
    ) -> None:
        stored = _labelled("confidential", regulatory_tags=["phi"], jurisdiction=["eu"])
        _seed(registry, stored)
        before = _version_count(registry)
        result = _run(registry, _route(route, unreadable), "restricted")
        assert result.status == CommandStatus.REJECTED
        assert "regulatory_tags, jurisdiction" in result.message
        assert NODE_ID in result.message
        assert _version_count(registry) == before
        assert _stored(registry)["data_classification"] == stored
        rejected = _events(registry, EventType.MUTATION_REJECTED)[-1]
        assert rejected.payload["error_code"] == "node_label_discards"
        # Only the seed's create was announced; the refused write announced nothing.
        assert len(_events(registry, EventType.ENTITY_CREATED)) == 1
        assert _events(registry, EventType.ENTITY_UPDATED) == []

    def test_over_a_stored_record_without_tags_it_is_written_as_before(
        self, registry: StoreRegistry, route: str, unreadable: object
    ) -> None:
        _seed(registry, _labelled("confidential", regulatory_tags=[]))
        _ok(_run(registry, _route(route, unreadable), "restricted"))
        assert _stored(registry)["data_classification"] == unreadable

    def test_a_readable_record_over_an_unreadable_stored_one_is_as_before(
        self, registry: StoreRegistry, route: str, unreadable: object
    ) -> None:
        _seed(registry, unreadable)
        supplied = _labelled("restricted", regulatory_tags=["pii"])
        _ok(_run(registry, _route(route, supplied), "restricted"))
        assert _stored(registry)["data_classification"] == supplied


# --- #1287 item 2: a malformed field on the winning supplied record ---
#
# A readable supplied record that wins or ties on sensitivity but holds a
# ``regulatory_tags`` or ``jurisdiction`` that is not a list of strings cannot
# be unioned with the stored one, so writing it would discard the stored value.
# It is refused as the level-less record is, but only when the stored record
# holds a value for that field.

MALFORMED = [None, "phi", ["phi", 3], {"tag": "phi"}]
FIELDS = ["regulatory_tags", "jurisdiction"]


def _batch(route: str, record: object) -> CommandBatch:
    """A bulk write: a neighbour create, then the label write, continuing on error."""
    neighbour = Command(
        operation=Operation.ENTITY_CREATE,
        args={
            "entity_id": "dataset:test:other",
            "entity_type": "dataset",
            "name": "other",
        },
        requested_by="tester",
    )
    return CommandBatch(
        commands=[neighbour, _route(route, record)],
        strategy=BatchStrategy.CONTINUE_ON_ERROR,
        requested_by="tester",
    )


def _run_via(
    registry: StoreRegistry, route: str, via: str, record: object
) -> CommandResult:
    executor = build_curate_executor(registry, clearance="restricted")
    if via == "single":
        return executor.execute(_route(route, record))
    results = executor.execute_batch(_batch(route, record))
    assert len(results) == 2
    assert results[0].status == CommandStatus.SUCCESS
    return results[-1]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("via", ["single", "batch"])
@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("malformed", MALFORMED)
class TestMalformedWinningField:
    @pytest.mark.parametrize("level", ["confidential", "restricted"])
    def test_over_a_stored_value_it_is_refused(
        self,
        registry: StoreRegistry,
        route: str,
        via: str,
        field: str,
        malformed: object,
        level: str,
    ) -> None:
        stored = _labelled("confidential", **{field: ["phi"]})
        _seed(registry, stored)
        before = _version_count(registry)
        supplied = _labelled(level, **{field: malformed})
        result = _run_via(registry, route, via, supplied)
        assert result.status == CommandStatus.REJECTED
        assert NODE_ID in result.message
        assert field in result.message
        assert _version_count(registry) == before
        assert _stored(registry)["data_classification"] == stored
        rejected = _events(registry, EventType.MUTATION_REJECTED)[-1]
        assert rejected.payload["error_code"] == "node_label_discards"
        assert len(_events(registry, EventType.ENTITY_CREATED)) == (
            1 if via == "single" else 2
        )
        assert _events(registry, EventType.ENTITY_UPDATED) == []

    def test_with_nothing_stored_for_it_it_is_written_as_supplied(
        self,
        registry: StoreRegistry,
        route: str,
        via: str,
        field: str,
        malformed: object,
    ) -> None:
        # Nothing to discard, so the winner is written as it stands, the
        # malformed value included (the behaviour before #1287).
        other = "jurisdiction" if field == "regulatory_tags" else "regulatory_tags"
        _seed(registry, _labelled("confidential", **{field: [], other: ["eu"]}))
        supplied = _labelled("confidential", **{field: malformed})
        _ok(_run_via(registry, route, via, supplied))
        assert _stored(registry)["data_classification"] == {
            **supplied,
            other: ["eu"],
        }

    def test_a_kept_stricter_stored_record_is_kept_as_before(
        self,
        registry: StoreRegistry,
        route: str,
        via: str,
        field: str,
        malformed: object,
    ) -> None:
        stored = _labelled("restricted", **{field: ["phi"]})
        _seed(registry, stored)
        supplied = _labelled("internal", **{field: malformed})
        _ok(_run_via(registry, route, via, supplied))
        assert _stored(registry)["data_classification"] == stored


@pytest.mark.parametrize("route", ROUTES)
def test_the_malformed_refusal_emits_the_level_less_refusal_event(
    tmp_path: Path, route: str
) -> None:
    def refusal(name: str, supplied: object) -> tuple[Any, str]:
        reg = StoreRegistry(stores_dir=tmp_path / name)
        try:
            _seed(reg, _labelled("confidential", regulatory_tags=["phi"]))
            result = _run(reg, _route(route, supplied), "restricted")
            assert result.status == CommandStatus.REJECTED
            events = reg.operational.event_log.get_events(entity_id=None, limit=1000)
            return events[-1], result.message
        finally:
            reg.close()

    level_less, _ = refusal("level-less", None)
    malformed, _ = refusal("malformed", _labelled("confidential", regulatory_tags=None))
    assert malformed.event_type == level_less.event_type == EventType.MUTATION_REJECTED
    assert set(malformed.payload) == set(level_less.payload)
    for key in set(level_less.payload) - {"command_id", "message", "errors"}:
        assert malformed.payload[key] == level_less.payload[key], key


@pytest.mark.parametrize("route", ROUTES)
def test_a_supplied_record_without_the_key_still_carries_the_stored_value(
    registry: StoreRegistry, route: str
) -> None:
    _seed(
        registry,
        _labelled("confidential", regulatory_tags=["phi"], jurisdiction=["eu"]),
    )
    _ok(_run(registry, _route(route, _labelled("restricted")), "restricted"))
    assert _stored(registry)["data_classification"] == {
        "sensitivity": "restricted",
        "regulatory_tags": ["phi"],
        "jurisdiction": ["eu"],
    }


# A stored field that is itself malformed: a write that leaves it as it stands
# discards nothing, so it is not refused (the #1293 review).
STORED_MALFORMED = [
    _labelled("confidential", jurisdiction="eu"),
    _labelled("confidential", regulatory_tags="phi"),
    _labelled("confidential", regulatory_tags=["phi", 3]),
]


@pytest.mark.parametrize("stored", STORED_MALFORMED)
def test_an_update_without_the_key_over_a_malformed_stored_field_succeeds(
    registry: StoreRegistry, stored: dict[str, Any]
) -> None:
    _seed(registry, stored)
    _ok(_run(registry, _update({"owner": "sales"}), "restricted"))
    props = _stored(registry)
    assert props["owner"] == "sales"
    assert props["data_classification"] == stored


@pytest.mark.parametrize("stored", STORED_MALFORMED)
def test_an_identical_recreate_over_a_malformed_stored_field_succeeds(
    registry: StoreRegistry, stored: dict[str, Any]
) -> None:
    _seed(registry, stored)
    _ok(_run(registry, _create({"data_classification": dict(stored)}), "restricted"))
    assert _stored(registry)["data_classification"] == stored


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("field", FIELDS)
def test_a_different_malformed_value_over_a_stored_list_is_refused(
    registry: StoreRegistry, route: str, field: str
) -> None:
    stored = _labelled("confidential", **{field: ["phi"]})
    _seed(registry, stored)
    result = _run(
        registry,
        _route(route, _labelled("confidential", **{field: "phi"})),
        "restricted",
    )
    assert result.status == CommandStatus.REJECTED
    assert _stored(registry)["data_classification"] == stored


@pytest.mark.parametrize("route", ROUTES)
def test_an_omitted_field_does_not_carry_a_malformed_stored_value(
    registry: StoreRegistry, route: str
) -> None:
    # As before #1287: only a list of strings is carried into a winner that
    # leaves the field out, so a malformed stored value is dropped, unrefused.
    _seed(registry, _labelled("confidential", jurisdiction="eu"))
    _ok(_run(registry, _route(route, _labelled("confidential")), "restricted"))
    assert "jurisdiction" not in _stored(registry)["data_classification"]


def _keep_stricter_node_label_before_1265(
    properties: dict[str, Any], stored_properties: Any, **_: Any
) -> None:
    """The #1263 rule as it stood before #1265, for the invariance test."""
    from trellis.schemas.classification import (
        DATA_CLASSIFICATION_KEY,
        sensitivity_of,
        sensitivity_rank,
    )

    if stored_properties is None or DATA_CLASSIFICATION_KEY not in stored_properties:
        return
    if sensitivity_rank(sensitivity_of(stored_properties)) > sensitivity_rank(
        sensitivity_of(properties)
    ):
        properties[DATA_CLASSIFICATION_KEY] = stored_properties[DATA_CLASSIFICATION_KEY]


def _replay_keyless_writes(registry: StoreRegistry) -> list[list[dict[str, Any]]]:
    """Writes that supply no label, on an unlabelled node and on labelled ones."""
    # An unlabelled node through every writer.
    _ok(_run(registry, _create({"owner": "sales"}), "restricted"))
    _ok(_run(registry, _update({"owner": "finance"}), "restricted"))
    _ok(_run(registry, _create({"owner": "ops"}), "restricted"))
    obs = Command(
        operation=Operation.OBSERVATION_RECORD,
        args={
            "observation": {
                **_observation("obs-1", NODE_ID),
                "observed_at": "2026-01-01T00:00:00+00:00",
            }
        },
        requested_by="tester",
    )
    meas = Command(
        operation=Operation.MEASUREMENT_RECORD,
        args={
            "measurement": {
                **_measurement("meas-1", NODE_ID),
                "measured_at": "2026-01-01T00:00:00+00:00",
            }
        },
        requested_by="tester",
    )
    for command in (obs, meas, obs, meas):
        _ok(_run(registry, command, "restricted"))
    # The record verbs again, over record nodes whose stored label holds tags
    # and a jurisdiction: those verbs never supply the key, so neither the
    # #1265 merge nor the #1287 refusal applies to them.
    graph = registry.knowledge.graph_store
    for node_id in ("obs-1", "meas-1"):
        node = graph.get_node(node_id)
        assert node is not None
        tagged = {
            "sensitivity": "confidential",
            "regulatory_tags": ["phi"],
            "jurisdiction": ["eu"],
        }
        props = {**node["properties"], "data_classification": tagged}
        graph.upsert_node(node_id, node["node_type"], props)
    for command in (obs, meas):
        _ok(_run(registry, command, "restricted"))
    # Labelled nodes (stored record with tags) written without the key.
    for level in ("public", "internal", "restricted"):
        node_id = f"dataset:test:{level}"
        record = {"sensitivity": level, "regulatory_tags": ["phi"]}
        graph.upsert_node(node_id, "dataset", {"data_classification": record})
        for command in (
            Command(
                operation=Operation.ENTITY_CREATE,
                args={
                    "entity_id": node_id,
                    "entity_type": "dataset",
                    "name": level,
                    "properties": {"owner": "x"},
                },
                requested_by="tester",
            ),
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={"entity_id": node_id, "properties": {"owner": "y"}},
                requested_by="tester",
            ),
        ):
            _ok(_run(registry, command, "restricted"))
    node_ids = [NODE_ID, "obs-1", "meas-1"] + [
        f"dataset:test:{level}" for level in ("public", "internal", "restricted")
    ]
    histories = [
        [
            {"node_type": v["node_type"], "properties": v["properties"]}
            for v in graph.get_node_history(node_id)
        ]
        for node_id in node_ids
    ]
    # The events too: type, entity and payload keys (values carry run ids).
    events = [
        [str(event.event_type), event.entity_id, sorted(event.payload)]
        for event in registry.operational.event_log.get_events(
            entity_id=None, limit=1000
        )
    ]
    return [*histories, events]


def test_writes_that_supply_no_label_are_unchanged_by_1265(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trellis.mutate.handlers import graph as graph_handlers

    after_reg = StoreRegistry(stores_dir=tmp_path / "after")
    try:
        after = _replay_keyless_writes(after_reg)
    finally:
        after_reg.close()
    monkeypatch.setattr(
        graph_handlers,
        "_keep_stricter_node_label",
        _keep_stricter_node_label_before_1265,
    )
    before_reg = StoreRegistry(stores_dir=tmp_path / "before")
    try:
        before = _replay_keyless_writes(before_reg)
    finally:
        before_reg.close()
    assert after == before
    assert all(len(history) >= 2 for history in after)
    assert "data_classification" not in after[0][-1]["properties"]
