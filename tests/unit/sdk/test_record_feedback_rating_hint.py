"""``record_feedback``'s out-of-range ``rating`` names the likely unit fix.

Both SDK clients construct a ``PackFeedbackRequest`` locally before any HTTP
call; pydantic's ``Field(ge=0.0, le=1.0)`` rejects an out-of-range rating
there, and :func:`trellis_sdk._http.rating_validation_hint` turns that
``ValidationError`` into a ``ValueError`` naming a ``/10``/``/100`` guess
when one is plausible. The mock transport's handler raises if called, so a
hinted rating never reaching the network is part of what each test proves.
"""

from __future__ import annotations

import httpx
import pytest

from trellis_sdk.async_client import AsyncTrellisClient
from trellis_sdk.client import TrellisClient


def _unreachable_handler(_request: httpx.Request) -> httpx.Response:
    msg = "no HTTP request should be made for a local rejection"
    raise AssertionError(msg)


class TestSyncRecordFeedbackRatingHint:
    @pytest.mark.parametrize(("rating", "guess"), [(7, 0.7), (85, 0.85)])
    def test_hint_names_the_hundred_or_ten_scale_guess(
        self, rating: float, guess: float
    ) -> None:
        http = httpx.Client(
            transport=httpx.MockTransport(_unreachable_handler),
            base_url="http://testserver",
        )
        with TrellisClient(http=http, verify_version=False) as client:
            with pytest.raises(ValueError, match=rf"did you mean {guess}\?") as exc:
                client.record_feedback(pack_id="p1", rating=rating)
            assert "rating must be between 0.0 and 1.0" in str(exc.value)

    def test_no_guess_re_raises_pydantics_own_error_unchanged(self) -> None:
        """No plausible unit: the original ``ValidationError`` passes through."""
        http = httpx.Client(
            transport=httpx.MockTransport(_unreachable_handler),
            base_url="http://testserver",
        )
        with TrellisClient(http=http, verify_version=False) as client:
            with pytest.raises(ValueError, match="rating") as exc:
                client.record_feedback(pack_id="p1", rating=-1.0)
            assert "did you mean" not in str(exc.value)
            assert type(exc.value).__name__ == "ValidationError"


class TestAsyncRecordFeedbackRatingHint:
    @pytest.mark.parametrize(("rating", "guess"), [(7, 0.7), (85, 0.85)])
    async def test_hint_names_the_hundred_or_ten_scale_guess(
        self, rating: float, guess: float
    ) -> None:
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(_unreachable_handler),
            base_url="http://testserver",
        )
        async with AsyncTrellisClient(http=http, verify_version=False) as client:
            with pytest.raises(ValueError, match=rf"did you mean {guess}\?") as exc:
                await client.record_feedback(pack_id="p1", rating=rating)
            assert "rating must be between 0.0 and 1.0" in str(exc.value)

    async def test_no_guess_re_raises_pydantics_own_error_unchanged(self) -> None:
        """No plausible unit: the original ``ValidationError`` passes through."""
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(_unreachable_handler),
            base_url="http://testserver",
        )
        async with AsyncTrellisClient(http=http, verify_version=False) as client:
            with pytest.raises(ValueError, match="rating") as exc:
                await client.record_feedback(pack_id="p1", rating=-1.0)
            assert "did you mean" not in str(exc.value)
            assert type(exc.value).__name__ == "ValidationError"
