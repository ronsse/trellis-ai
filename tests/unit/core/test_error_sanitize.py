"""Leak heuristics for machine-readable error surfaces (issue #206)."""

from __future__ import annotations

from trellis.core.error_sanitize import (
    SUPPRESSED_MARKER,
    sanitize_error_message,
    sanitized_error_payload,
)


class TestCleanPassthrough:
    def test_operator_authored_message_passes_through(self) -> None:
        msg = "entity_type 'precedent' not registered"
        assert sanitize_error_message(msg) == msg

    def test_config_error_prose_passes_through(self) -> None:
        # "password must be set" is prose, not an assignment — stays clean.
        msg = "dsn must be set for postgres backend (config or env var)"
        assert sanitize_error_message(msg) == msg
        msg2 = "password must be set for the neo4j backend"
        assert sanitize_error_message(msg2) == msg2

    def test_file_paths_and_dotted_modules_pass_through(self) -> None:
        # Dots and slashes break token-shaped runs — paths stay clean.
        msg = (
            "No such file: /home/user/projects/trellis-ai/build/artifacts/run.json "
            "(raised in trellis.stores.registry._instantiate)"
        )
        assert sanitize_error_message(msg) == msg

    def test_ulid_passes_through(self) -> None:
        # ULIDs are 26 chars — under the 40-char token threshold.
        msg = "node 01JGME6CE1RJ0S4W5X7Y8Z9ABC has no current version"
        assert sanitize_error_message(msg) == msg


class TestSuppression:
    def test_email_suppressed(self) -> None:
        msg = "saved query owned by jane.doe@example.com failed validation"
        assert sanitize_error_message(msg) == SUPPRESSED_MARKER

    def test_url_with_credentials_suppressed(self) -> None:
        # The classic driver leak: connection error echoing the DSN.
        msg = (
            'connection failed: could not connect to server at '
            '"postgresql://trellis:s3cretpw@db.internal:5432/prod"'
        )
        assert sanitize_error_message(msg) == SUPPRESSED_MARKER

    def test_secret_assignment_suppressed(self) -> None:
        assert sanitize_error_message("auth failed: api_key=sk-abc123") == (
            SUPPRESSED_MARKER
        )
        assert sanitize_error_message("header Authorization: Bearer xyz") == (
            SUPPRESSED_MARKER
        )

    def test_long_token_run_suppressed(self) -> None:
        token = "A" * 20 + "b1" * 12  # 44-char unbroken run
        assert sanitize_error_message(f"request rejected: {token}") == (
            SUPPRESSED_MARKER
        )

    def test_raw_sql_suppressed(self) -> None:
        msg = (
            "query failed: SELECT user_id, vendor_user_id FROM "
            "landing.application_events.events WHERE 1=1"
        )
        assert sanitize_error_message(msg) == SUPPRESSED_MARKER

    def test_insert_statement_suppressed(self) -> None:
        assert sanitize_error_message(
            "syntax error near: INSERT INTO staging.users VALUES (1)"
        ) == SUPPRESSED_MARKER


class TestBounding:
    def test_long_clean_message_truncated(self) -> None:
        msg = "x " * 400  # clean but way over the bound
        out = sanitize_error_message(msg)
        assert out.endswith("…[truncated]")
        assert len(out) < len(msg)

    def test_custom_max_len(self) -> None:
        out = sanitize_error_message("abcdef", max_len=3)
        assert out == "abc…[truncated]"


class TestPayload:
    def test_payload_shape(self) -> None:
        payload = sanitized_error_payload(ValueError("bad input"), command="ingest")
        assert payload == {
            "status": "error",
            "error_type": "ValueError",
            "message": "bad input",
            "command": "ingest",
        }

    def test_payload_suppresses_sensitive_detail_keeps_type(self) -> None:
        exc = RuntimeError("token: sk-live-abcdef refused")
        payload = sanitized_error_payload(exc)
        # error_type survives (a class name never carries payload data);
        # the message does not.
        assert payload["error_type"] == "RuntimeError"
        assert payload["message"] == SUPPRESSED_MARKER
