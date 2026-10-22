"""``LearningPromotionResponse.status`` names the verdicts the route can return.

The route answers 200 whatever the run's verdict, so a client reads the
outcome from ``status``. Typed as a plain ``str`` the OpenAPI spec listed no
values and a stray string validated; typed as the same literal the shared
verdict function returns, the spec enumerates them and the DTO refuses the rest.
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from trellis.learning.scoring import PromotionVerdict
from trellis_api.app import create_app
from trellis_wire.dtos import LearningPromotionResponse

VERDICTS = ["ok", "refused", "failed"]


def _status_schema() -> dict[str, object]:
    spec = create_app().openapi()
    return spec["components"]["schemas"]["LearningPromotionResponse"]["properties"][
        "status"
    ]


def test_the_openapi_spec_lists_every_verdict() -> None:
    assert _status_schema()["enum"] == VERDICTS


def test_the_dto_and_the_verdict_function_share_one_vocabulary() -> None:
    annotation = LearningPromotionResponse.model_fields["status"].annotation

    assert set(get_args(annotation)) == set(get_args(PromotionVerdict)) == set(VERDICTS)


@pytest.mark.parametrize("verdict", VERDICTS)
def test_each_verdict_validates(verdict: str) -> None:
    assert LearningPromotionResponse(status=verdict).status == verdict


def test_the_default_is_ok() -> None:
    assert LearningPromotionResponse().status == "ok"


def test_a_stray_status_is_refused() -> None:
    with pytest.raises(ValidationError):
        LearningPromotionResponse(status="promoted")
