"""Leak heuristics for machine-readable error surfaces (issue #206)."""

from __future__ import annotations

import time

import pytest
import yaml

from trellis.core.error_sanitize import (
    DEFAULT_MAX_LEN,
    SUPPRESSED_MARKER,
    describe_yaml_error,
    sanitize_error_message,
    sanitized_error_payload,
)

#: A fake credential planted where PyYAML's ``str(exc)`` would quote it.
_SENTINEL = "XQ1BR6asQyYAJ6tcK6JnaWKZ"


def _filler(length: int) -> str:
    """``length`` chars of prose: short space-separated words, so no run
    is 40+ chars and nothing resembles ``@``/``=``/``:`` — safe against
    every ``_LEAK_PATTERNS`` entry and the long-opaque-token check."""
    return ("lorem " * (length // 6 + 2))[:length]


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
                'property `claim_key` = \'["synthetic-system","synthetic-raw-id"]\'} '
                "{gql_status: 22N79}"
            ),
            # ArcadeDB 26.8.1 over Bolt, raised inside a managed transaction
            # with the value passed as a bound parameter (as the Bolt store
            # itself issues writes) against an already-committed duplicate
            # (gql_status 50N42). The index name carries the label and
            # property; the record id varies per run. An alias claim key is
            # the store's JSON array, so that value holds its own brackets.
            (
                "{neo4j_code: Neo.ClientError.Transaction.TransactionNotFound} "
                "{message: Duplicated key [synthetic-version-dup] found on "
                "index 'Node[version_id]' already assigned to record #1:0} "
                "{gql_status: 50N42}"
            ),
            (
                "{neo4j_code: Neo.ClientError.Transaction.TransactionNotFound} "
                '{message: Duplicated key [["synthetic-system","synthetic-raw-id"]] '
                "found on index 'AliasClaim[claim_key]' already assigned to "
                "record #9:1} {gql_status: 50N42}"
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
        # The Neo4j wording up to the property name, with no quoted value.
        msg = (
            "entity_type 'precedent' already exists with label `Tag` and "
            "property `name` is already set"
        )
        assert sanitize_error_message(msg) == msg

    @pytest.mark.parametrize(
        "msg",
        [
            # Neo4j 2025.12 ``str(Neo4jError)``, raised by the stores' own
            # startup schema DDL (``CREATE CONSTRAINT ... IS UNIQUE``) run over
            # two synthetic nodes that already duplicate the value (gql_status
            # 50N11). Distinct from the write-time violation above. The two
            # cases differ in label and property; the claim key is the store's
            # JSON array.
            (
                "{neo4j_code: Neo.DatabaseError.Schema.ConstraintCreationFailed} "
                "{message: Unable to create Constraint( "
                "name='node_version_unique', type='NODE PROPERTY UNIQUENESS', "
                "schema=(:Node {version_id}) ):\n"
                "Both Node(0) and Node(1) have the label `Node` and property "
                "`version_id` = 'synthetic-constraint-dup'. Note that only the "
                "first found violation is shown.} {gql_status: 50N11} "
                "{gql_status_description: error: general processing exception - "
                "constraint creation failed. Unable to create "
                "'node_version_unique'.}"
            ),
            (
                "{neo4j_code: Neo.DatabaseError.Schema.ConstraintCreationFailed} "
                "{message: Unable to create Constraint( "
                "name='alias_claim_unique', type='NODE PROPERTY UNIQUENESS', "
                "schema=(:AliasClaim {claim_key}) ):\n"
                "Both Node(2) and Node(3) have the label `AliasClaim` and "
                'property `claim_key` = \'["synthetic-system","synthetic-raw-id"]\'. '
                "Note that only the first found violation is shown.} "
                "{gql_status: 50N11} {gql_status_description: error: general "
                "processing exception - constraint creation failed. Unable to "
                "create 'alias_claim_unique'.}"
            ),
        ],
        ids=["node-version-unique", "alias-claim-unique"],
    )
    def test_neo4j_constraint_creation_over_duplicates_suppressed(
        self, msg: str
    ) -> None:
        assert sanitize_error_message(msg) == SUPPRESSED_MARKER

    def test_constraint_creation_without_quoted_value_passes_through(self) -> None:
        # The "have the label" wording up to the property name, with no
        # quoted value — the near-miss control for the pattern above.
        msg = (
            "Both Node(0) and Node(1) have the label `Node` and property "
            "`version_id` set but no single violation could be reported"
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


class TestScanWindowBound:
    """``sanitize_error_message`` scans a bounded window, not the whole
    text (#763 follow-up 2): the email and credential-URL patterns
    backtrack quadratically on a long run of word characters, so an
    unbounded scan of caller-controlled exception text can stall a
    request for seconds to minutes."""

    def test_long_adversarial_run_sanitizes_well_under_a_second(self) -> None:
        # 100k chars in the URL-reaching shape; the gate measured base at
        # ~27s for this size (quadratic in text length). The window bound
        # makes the scan length independent of the input, so head should
        # finish in well under the 1s bound even on a loaded CI runner.
        text = "x" * 100_000 + "://"
        start = time.perf_counter()
        out = sanitize_error_message(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"took {elapsed:.3f}s — the scan is not bounded"
        # The 100k-char run is itself a long opaque token either way.
        assert out == SUPPRESSED_MARKER

    def test_email_straddling_the_cut_is_suppressed(self) -> None:
        # The local part starts 5 chars before max_len; "@example.com"
        # falls entirely past it, within the scan margin.
        filler = _filler(DEFAULT_MAX_LEN - 5)
        text = filler + "alice@example.com" + _filler(50)
        assert sanitize_error_message(text) == SUPPRESSED_MARKER

    def test_credential_url_straddling_the_cut_is_suppressed(self) -> None:
        # "postgres:/" (10 chars) lands before max_len; the credential and
        # its closing "@" fall past it, within the scan margin.
        filler = _filler(DEFAULT_MAX_LEN - 10)
        text = filler + "postgres://admin:hunter2pass@" + _filler(50)
        assert sanitize_error_message(text) == SUPPRESSED_MARKER

    def test_leak_wholly_beyond_the_window_passes_through_clean(self) -> None:
        # The secret starts 200 chars past max_len — comfortably past any
        # reasonable scan margin — so no part of it is in the visible
        # prefix and no part of it can leak through sanitize_error_message's
        # output: the window bound's intended, documented consequence
        # (see PR body). At base (unbounded scan) this is suppressed
        # instead, since the full text is searched regardless of max_len.
        leaked_email = "alice@example.com"
        filler = _filler(DEFAULT_MAX_LEN + 200)
        text = filler + leaked_email
        out = sanitize_error_message(text)
        assert out == text[:DEFAULT_MAX_LEN] + "…[truncated]"
        assert leaked_email not in out
        assert "alice" not in out
        assert "@" not in out


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
