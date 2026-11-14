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
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.schemas.classification import DataClassification
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
