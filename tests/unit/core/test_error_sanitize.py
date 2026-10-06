"""Leak heuristics for machine-readable error surfaces (issue #206)."""

from __future__ import annotations

import pytest
import yaml

from trellis.core.error_sanitize import (
    SUPPRESSED_MARKER,
    describe_yaml_error,
    sanitize_error_message,
    sanitized_error_payload,
)

#: A fake credential planted where PyYAML's ``str(exc)`` would quote it.
_SENTINEL = "XQ1BR6asQyYAJ6tcK6JnaWKZ"


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

    @pytest.mark.parametrize("component_len", [39, 40])
    def test_long_safe_path_component_passes_through(self, component_len: int) -> None:
        msg = f"cannot read /tmp/{'a' * component_len}/資料/policies.json"
        assert sanitize_error_message(msg) == msg

    def test_ulid_passes_through(self) -> None:
        # ULIDs are 26 chars — under the 40-char token threshold.
        msg = "node 01JGME6CE1RJ0S4W5X7Y8Z9ABC has no current version"
        assert sanitize_error_message(msg) == msg

    def test_text_near_the_postgres_row_shape_passes_through(self) -> None:
        msg = "Key (name) is not indexed"
        assert sanitize_error_message(msg) == msg


class TestSuppression:
    def test_email_suppressed(self) -> None:
        msg = "saved query owned by jane.doe@example.com failed validation"
        assert sanitize_error_message(msg) == SUPPRESSED_MARKER

    def test_url_with_credentials_suppressed(self) -> None:
        # The classic driver leak: connection error echoing the DSN.
        msg = (
            "connection failed: could not connect to server at "
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

    def test_exactly_forty_character_token_is_suppressed(self) -> None:
        assert sanitize_error_message("A1" * 20) == SUPPRESSED_MARKER

    def test_secret_assignment_inside_path_is_suppressed(self) -> None:
        msg = "cannot read /srv/deploy/token=hunter2/policies.json"
        assert sanitize_error_message(msg) == SUPPRESSED_MARKER

    @pytest.mark.parametrize(
        "template",
        [
            "cannot read /srv/{token}/policies.json",
            r"cannot read C:\secrets\{token}\config.json",
            "request failed: https://api.example.com/v1/{token}/result",
            "cannot read s3://bucket/{token}/object",
        ],
    )
    def test_secret_shaped_path_component_is_suppressed(self, template: str) -> None:
        message = template.format(token="A1" * 20)
        assert sanitize_error_message(message) == SUPPRESSED_MARKER

    @pytest.mark.parametrize("host", ["localhost", "db", "10.0.0.1", "[::1]"])
    def test_token_only_url_userinfo_is_suppressed(self, host: str) -> None:
        userinfo = "short-token"
        assert (
            sanitize_error_message(f"https://{userinfo}@{host}/path")
            == SUPPRESSED_MARKER
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://localhost?revision=abc@main",
            "https://localhost#revision=abc@main",
        ],
    )
    def test_at_sign_outside_url_authority_passes_through(self, url: str) -> None:
        assert sanitize_error_message(url) == url

    def test_raw_sql_suppressed(self) -> None:
        msg = (
            "query failed: SELECT user_id, vendor_user_id FROM "
            "landing.application_events.events WHERE 1=1"
        )
        assert sanitize_error_message(msg) == SUPPRESSED_MARKER

    def test_insert_statement_suppressed(self) -> None:
        assert (
            sanitize_error_message(
                "syntax error near: INSERT INTO staging.users VALUES (1)"
            )
            == SUPPRESSED_MARKER
        )

    @pytest.mark.parametrize(
        "msg",
        [
            # ``str(exc)`` of psycopg 3.3 errors from PostgreSQL 16, with
            # synthetic values. The DETAIL line quotes the offending row.
            (
                'duplicate key value violates unique constraint "pair_a_b_key"\n'
                "DETAIL:  Key (a, b)=(1, synthetic b value) already exists."
            ),
            (
                "duplicate key value violates unique constraint"
                ' "widget_lower_label"\n'
                "DETAIL:  Key (lower(label))=(synthetic label) already exists."
            ),
            (
                'insert or update on table "widget" violates foreign key constraint'
                ' "widget_parent_id_fkey"\n'
                "DETAIL:  Key (parent_id)=(424242) is not present in table"
                ' "parent".'
            ),
            (
                'null value in column "label" of relation "widget" violates'
                " not-null constraint\n"
                "DETAIL:  Failing row contains (5, synthetic-name-5, null, 1,"
                " null)."
            ),
        ],
        ids=["unique-composite", "unique-expression", "foreign-key-insert", "not-null"],
    )
    def test_postgres_row_values_suppressed(self, msg: str) -> None:
        assert sanitize_error_message(msg) == SUPPRESSED_MARKER

    @pytest.mark.parametrize(
        "msg",
        [
            # Neo4j 2025.12 ``str(Neo4jError)`` over Bolt, captured against
            # Trellis's own constraint DDL with synthetic values (gql_status
            # 22N79). One per declared constraint label.
            (
                "{neo4j_code: Neo.ClientError.Schema.ConstraintValidationFailed} "
                "{message: Node(0) already exists with label `Node` and "
                "property `version_id` = 'synthetic-version-dup'} "
                "{gql_status: 22N79}"
            ),
            (
                "{neo4j_code: Neo.ClientError.Schema.ConstraintValidationFailed} "
                "{message: Node(1) already exists with label `AliasClaim` and "
                "property `claim_key` = 'synthetic-system|synthetic-raw-id'} "
                "{gql_status: 22N79}"
            ),
            # ArcadeDB 26.8.1 over Bolt, raised inside a managed transaction
            # with the value passed as a bound parameter (as the Bolt store
            # itself issues writes) against an already-committed duplicate
            # (gql_status 50N42). The index name carries the label and
            # property; the record id varies per run.
            (
                "{neo4j_code: Neo.ClientError.Transaction.TransactionNotFound} "
                "{message: Duplicated key [synthetic-version-dup] found on "
                "index 'Node[version_id]' already assigned to record #1:0} "
                "{gql_status: 50N42}"
            ),
            (
                "{neo4j_code: Neo.ClientError.Transaction.TransactionNotFound} "
                "{message: Duplicated key [synthetic-system|synthetic-raw-id] "
                "found on index 'AliasClaim[claim_key]' already assigned to "
                "record #9:0} {gql_status: 50N42}"
            ),
        ],
        ids=[
            "neo4j-node-version-unique",
            "neo4j-alias-claim-unique",
            "arcadedb-node-tx",
            "arcadedb-alias-claim-tx",
        ],
    )
    def test_bolt_duplicate_constraint_values_suppressed(self, msg: str) -> None:
        assert sanitize_error_message(msg) == SUPPRESSED_MARKER

    def test_already_exists_without_quoted_value_passes_through(self) -> None:
        # Near-miss: a Trellis-authored message that mentions "already
        # exists" and even echoes the Neo4j "with label ... and property"
        # wording, but never reaches a quoted value — must stay clean.
        msg = (
            "entity_type 'precedent' already exists with label `Tag` and "
            "property `name` is already set"
        )
        assert sanitize_error_message(msg) == msg


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


class TestDescribeYamlError:
    """A config line can hold a password, so a parse error never quotes one."""

    @pytest.mark.parametrize(
        ("document", "expected"),
        [
            ("a: *{S}\n", "found undefined alias '...' (line 1, column 4)"),
            (
                "a: !!python/name:os.{S}\n",
                (
                    "could not determine a constructor for the tag '...'"
                    " (line 1, column 4)"
                ),
            ),
            (
                "a: &{S} 1\nb: &{S} 2\n",
                (
                    "found duplicate anchor '...'; first occurrence (line 1, column 4);"
                    " second occurrence (line 2, column 4)"
                ),
            ),
            ("a: b: {S}\n", "mapping values are not allowed here (line 1, column 5)"),
        ],
        ids=["alias", "tag", "anchor", "value"],
    )
    def test_document_text_is_masked_and_positions_count_from_one(
        self, document: str, expected: str
    ) -> None:
        with pytest.raises(yaml.YAMLError) as exc:
            yaml.safe_load(document.replace("{S}", _SENTINEL))
        assert describe_yaml_error(exc.value) == expected

    def test_a_quoted_character_or_parser_token_is_kept(self) -> None:
        with pytest.raises(yaml.YAMLError) as tab:
            yaml.safe_load("a:\n\tb: c\n")
        with pytest.raises(yaml.YAMLError) as flow:
            yaml.safe_load("a: [b\n")

        assert "found character '\\t' that cannot start any token" in (
            describe_yaml_error(tab.value)
        )
        assert describe_yaml_error(flow.value) == (
            "while parsing a flow sequence (line 1, column 4); expected ',' or"
            " ']', but got '<stream end>' (line 2, column 1)"
        )

    def test_a_reader_error_names_the_code_point_and_offset(self) -> None:
        with pytest.raises(yaml.YAMLError) as exc:
            yaml.safe_load("a: \x07\n")
        assert describe_yaml_error(exc.value) == (
            "unacceptable character #x0007 at position 3"
        )

    @pytest.mark.parametrize("tag", ["!!int", "!!float", "!!bool"])
    def test_a_tag_that_cannot_construct_is_named_by_type_alone(self, tag: str) -> None:
        # Not YAMLError: ``int()`` / ``float()`` raise ValueError and the bool
        # table a KeyError, each quoting the value (``!!bool`` lowercased).
        with pytest.raises((ValueError, KeyError)) as exc:
            yaml.safe_load(f"a: {tag} {_SENTINEL}\n")
        described = describe_yaml_error(exc.value)
        assert described.startswith("a value could not be constructed (")
        assert _SENTINEL.lower() not in described.lower()
