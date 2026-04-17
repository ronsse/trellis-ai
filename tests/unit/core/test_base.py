"""Tests for base Pydantic models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError as PydanticValidationError

from trellis.core.base import TimestampedModel, TrellisModel, VersionedModel, utc_now


def test_utc_now_returns_utc_datetime():
    now = utc_now()
    assert isinstance(now, datetime)
    assert now.tzinfo is not None
    assert now.tzinfo == UTC


def test_xpmodel_forbids_extra_fields():
    class MyModel(TrellisModel):
        name: str

    with pytest.raises(PydanticValidationError):
        MyModel(name="test", unexpected="bad")


def test_versioned_model_has_schema_version():
    class MyVersioned(VersionedModel):
        pass

    obj = MyVersioned()
    assert isinstance(obj.schema_version, str)
    assert obj.schema_version == "0.1.0"


def test_timestamped_model_has_created_and_updated():
    class MyTimestamped(TimestampedModel):
        pass

    obj = MyTimestamped()
    assert isinstance(obj.created_at, datetime)
    assert isinstance(obj.updated_at, datetime)
    assert obj.created_at.tzinfo == UTC
    assert obj.updated_at.tzinfo == UTC
