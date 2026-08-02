"""Tests for the Cohort-2 diff-level guardrails.

These are the controls that make an unattended authoring cycle safe, so
they are tested adversarially: the interesting cases are the ones where
a proposal *tries* to reach past its scope.
"""

from __future__ import annotations

import pytest

from trellis_workers.code_authoring.safety import (
    AllowlistError,
    is_hard_excluded,
    scan_secrets,
    validate_allowlist,
    verify_diff_allowlist,
)


class TestGlobMatching:
    def test_single_star_does_not_cross_directories(self) -> None:
        # The allow direction must stay narrow: 'src/*.py' is not a
        # licence to edit 'src/trellis/deep/thing.py'.
        assert verify_diff_allowlist(("src/trellis/thing.py",), ("src/*.py",))

    def test_double_star_crosses_directories(self) -> None:
        assert not verify_diff_allowlist(("src/trellis/a/b/thing.py",), ("src/**",))

    def test_double_star_matches_the_directory_itself(self) -> None:
        assert is_hard_excluded("src/trellis_api/auth")


class TestHardExclusions:
    @pytest.mark.parametrize(
        "path",
        [
            "src/trellis_api/auth.py",
            "src/trellis_api/auth/keys.py",
            "src/trellis/mutate/policies/scope.py",
            "src/trellis/mutate/executor.py",
            "src/trellis/stores/registry.py",
            ".github/workflows/tests.yml",
            "src/trellis/thing_security_gate.py",
            "config/secrets.yaml",
            ".env",
            "deploy/.env.production",
        ],
    )
    def test_protected_paths_are_excluded(self, path: str) -> None:
        assert is_hard_excluded(path)

    def test_ordinary_source_is_not_excluded(self) -> None:
        assert not is_hard_excluded("src/trellis/retrieve/excerpts.py")

    def test_broad_glob_cannot_swallow_an_excluded_path(self) -> None:
        # The bidirectional check: 'src/trellis/**' would otherwise buy
        # write access to executor.py by being broader than the exclusion.
        with pytest.raises(AllowlistError, match="hard exclusion"):
            validate_allowlist(("src/trellis/**",))


class TestValidateAllowlist:
    def test_accepts_a_narrow_allowlist(self) -> None:
        validate_allowlist(
            ("src/trellis/retrieve/excerpts.py", "tests/unit/retrieve/**")
        )

    def test_rejects_empty(self) -> None:
        with pytest.raises(AllowlistError, match="empty"):
            validate_allowlist(())

    @pytest.mark.parametrize("glob", ["../../etc/passwd", "src/../../../secrets"])
    def test_rejects_traversal(self, glob: str) -> None:
        with pytest.raises(AllowlistError, match="traversal"):
            validate_allowlist((glob,))

    @pytest.mark.parametrize("glob", ["/etc/passwd", "~/.ssh/id_rsa"])
    def test_rejects_non_relative(self, glob: str) -> None:
        with pytest.raises(AllowlistError, match="non-relative"):
            validate_allowlist((glob,))

    def test_rejects_blank_entry(self) -> None:
        with pytest.raises(AllowlistError, match="blank"):
            validate_allowlist(("src/a.py", "   "))


class TestVerifyDiffAllowlist:
    def test_permitted_diff_yields_no_violations(self) -> None:
        assert (
            verify_diff_allowlist(
                ("src/trellis/retrieve/excerpts.py", "tests/unit/retrieve/test_x.py"),
                ("src/trellis/retrieve/*.py", "tests/unit/retrieve/**"),
            )
            == ()
        )

    def test_reports_every_violation_not_just_the_first(self) -> None:
        violations = verify_diff_allowlist(
            ("a.py", "b.py", "src/trellis/retrieve/ok.py"),
            ("src/trellis/retrieve/*.py",),
        )
        assert {v.path for v in violations} == {"a.py", "b.py"}

    def test_hard_exclusion_wins_over_a_matching_glob(self) -> None:
        # Even if the (already-validated) allowlist somehow matches, an
        # excluded path can never ride through on the diff side.
        violations = verify_diff_allowlist(
            ("src/trellis/mutate/executor.py",),
            ("src/trellis/mutate/executor.py",),
        )
        assert len(violations) == 1


class TestScanSecrets:
    @pytest.mark.parametrize(
        ("name", "line"),
        [
            ("aws_access_key_id", "+AKIAIOSFODNN7EXAMPLE"),
            ("openai_api_key", "+key = 'sk-abcdefghijklmnopqrstuvwxyz0123456789'"),
            (
                "anthropic_api_key",
                "+tok = 'sk-ant-abcdefghijklmnopqrstuvwxyz0123456789'",
            ),
            ("password_assignment", '+password = "hunter2xyz"'),
            ("bearer_token_literal", "+h = 'Bearer abcdefghijklmnopqrstuvwxyz123'"),
            ("private_key_block", "+-----BEGIN RSA PRIVATE KEY-----"),
        ],
    )
    def test_detects_secret_shapes(self, name: str, line: str) -> None:
        matches = scan_secrets(line)
        assert name in {m.pattern_name for m in matches}

    def test_ignores_context_and_removed_lines(self) -> None:
        # A secret being *deleted* is a fix, not a leak.
        assert scan_secrets("-AKIAIOSFODNN7EXAMPLE\n AKIAIOSFODNN7EXAMPLE") == ()

    def test_ignores_the_file_header(self) -> None:
        assert scan_secrets("+++ b/.env.example") == ()

    def test_clean_diff_is_clean(self) -> None:
        assert scan_secrets("+def add(a, b):\n+    return a + b") == ()

    def test_match_is_redacted_in_the_preview(self) -> None:
        (match,) = scan_secrets("+AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in match.line_preview
        assert "***" in match.line_preview
        assert match.line_number == 1
