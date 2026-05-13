"""SDK tests for record_observation / query_observations + Measurement.

Item 1 Phase 1. Uses the in-process FastAPI shim
(:func:`trellis.testing.in_memory_client`) for a fast round-trip without
a real network listener.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.testing import in_memory_client


@pytest.fixture
def client(tmp_path: Path):
    with in_memory_client(tmp_path / "stores") as c:
        yield c


def _make_subject(client) -> str:
    """Create a subject entity for the observation to attach to."""
    return client.create_entity("users", entity_type="Dataset")


class TestObservationsSDK:
    def test_record_and_query_round_trip(self, client) -> None:
        subject_id = _make_subject(client)
        observation_id = client.record_observation(
            {
                "subject_entity_id": subject_id,
                "subject_entity_type": "Dataset",
                "observer_agent_id": "agent-1",
                "content": "filter-projection asymmetry",
                "confidence": 0.7,
            }
        )
        assert observation_id

        results = client.query_observations(subject_entity_id=subject_id)
        assert len(results) == 1
        assert results[0]["observation_id"] == observation_id
        assert results[0]["content"] == "filter-projection asymmetry"

    def test_query_filter_by_observer(self, client) -> None:
        subject_id = _make_subject(client)
        client.record_observation(
            {
                "subject_entity_id": subject_id,
                "subject_entity_type": "Dataset",
                "observer_agent_id": "agent-a",
                "content": "a",
                "confidence": 0.5,
            }
        )
        client.record_observation(
            {
                "subject_entity_id": subject_id,
                "subject_entity_type": "Dataset",
                "observer_agent_id": "agent-b",
                "content": "b",
                "confidence": 0.5,
            }
        )

        a_results = client.query_observations(observer_agent_id="agent-a")
        assert len(a_results) == 1
        assert a_results[0]["observer_agent_id"] == "agent-a"

    def test_missing_required_field_raises_422(self, client) -> None:
        """Missing required field hits server-side validation. The SDK
        raises a TrellisAPIError, not a silent default."""
        from trellis_sdk.exceptions import TrellisAPIError

        with pytest.raises(TrellisAPIError):
            client.record_observation(
                {
                    "subject_entity_id": "ds-1",
                    "subject_entity_type": "Dataset",
                    # missing observer_agent_id, content, confidence
                }
            )


class TestSDKToMCPConsistency:
    """End-to-end check: SDK record → MCP query sees the same observation.

    Both surfaces resolve through the same MutationExecutor → graph store
    → query layer, so any drift between them would surface here. The MCP
    tool is invoked directly (unwrapping the FastMCP wrapper) so we
    don't need a JSON-RPC transport for this in-process check.
    """

    def test_sdk_record_visible_via_mcp_query(self, tmp_path: Path) -> None:
        import trellis.mcp.server as mcp_server_module

        subject_id_holder: dict[str, str] = {}
        observation_id_holder: dict[str, str] = {}

        with in_memory_client(tmp_path / "stores") as sdk_client:
            # The MCP server module reads from its own _registry global —
            # point it at the same registry the SDK is writing through.
            import trellis_api.app as api_app_module

            mcp_server_module._registry = api_app_module._registry

            subject_id = sdk_client.create_entity("users", entity_type="Dataset")
            subject_id_holder["id"] = subject_id

            observation_id = sdk_client.record_observation(
                {
                    "subject_entity_id": subject_id,
                    "subject_entity_type": "Dataset",
                    "observer_agent_id": "agent-1",
                    "content": "shared content",
                    "confidence": 0.6,
                }
            )
            observation_id_holder["id"] = observation_id

            # Now exercise the MCP query tool against the same registry.
            from tests.unit.mcp.conftest import unwrap_tool

            query_fn = unwrap_tool(mcp_server_module.query_observations)
            import json as _json

            result = _json.loads(query_fn(subject_entity_id=subject_id))
            assert result["status"] == "ok"
            ids = {row["observation_id"] for row in result["observations"]}
            assert observation_id in ids

            mcp_server_module._registry = None


class TestMeasurementsSDK:
    def test_record_and_query_round_trip(self, client) -> None:
        subject_id = _make_subject(client)
        measurement_id = client.record_measurement(
            {
                "subject_entity_id": subject_id,
                "subject_entity_type": "Dataset",
                "metric_name": "null_rate",
                "metric_value": 0.03,
                "unit": "percent",
                "observer_agent_id": "agent-1",
            }
        )
        assert measurement_id

        rows = client.query_measurements(metric_name="null_rate")
        assert len(rows) == 1
        assert rows[0]["measurement_id"] == measurement_id
        assert rows[0]["metric_value"] == pytest.approx(0.03)
