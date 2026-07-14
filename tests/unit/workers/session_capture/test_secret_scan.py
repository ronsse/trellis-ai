"""Each secret-scan class catches its synthetic example; clean text passes.

Every secret example is synthetic and assembled from parts so no literal
credential (or repo-scanner trigger substring) is committed.
"""

from __future__ import annotations

from trellis_workers.session_capture import secret_scan


def test_key_value_secret_class() -> None:
    text = "config uses api_key=" + "sk_live_ABC123def456GHI789"
    assert "key_value_secret" in secret_scan.scan(text)
    assert secret_scan.contains_secret(text)


def test_bearer_token_class() -> None:
    text = "sent header Authorization: Bearer " + "aB3dE5fG7hJ9kL1mN3pQ"
    assert "bearer_token" in secret_scan.scan(text)


def test_op_ref_class() -> None:
    # Assemble the scheme from parts so the literal never lands in the file.
    ref = "op:" + "//FakeVault/fake-item/password"
    text = f"the runbook read {ref} at deploy time"
    assert "op_ref" in secret_scan.scan(text)


def test_pem_private_key_class() -> None:
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIfake...\n-----END RSA PRIVATE KEY-----"
    assert "pem_private_key" in secret_scan.scan(text)


def test_connection_string_class() -> None:
    # Verified miss for the entropy heuristic: the :/@ delimiters split the
    # DSN below the token-length gate. Synthetic credentials throughout.
    text = "connect via postgres" + "://admin:hunter2@db.fake.internal:5432/prod"
    assert "connection_string" in secret_scan.scan(text)


def test_connection_string_requires_password_segment() -> None:
    # A plain URL and a passwordless userinfo must NOT match.
    assert "connection_string" not in secret_scan.scan(
        "see https://docs.example.test:8080/guide for details"
    )
    assert "connection_string" not in secret_scan.scan(
        "clone from ssh://git@code.example.test/repo.git"
    )


def test_aws_access_key_id_class() -> None:
    # The AWS docs' canonical fake key — entropy ~3.7 sits under the 4.0
    # threshold, hence the named class.
    text = "found key AKIA" + "IOSFODNN7EXAMPLE in the old config"
    assert "aws_access_key_id" in secret_scan.scan(text)


def test_aws_temporary_key_id_class() -> None:
    text = "session used ASIA" + "IOSFODNN7EXAMPLE briefly"
    assert "aws_access_key_id" in secret_scan.scan(text)


def test_high_entropy_string_class() -> None:
    blob = "kJ8xQ2vB9nM4wZ7pR1sT6yU3aC5dF0gH2jL4kN6mP8"
    text = f"the response returned {blob} as an access token"
    assert secret_scan.HIGH_ENTROPY_CLASS in secret_scan.scan(text)


def test_clean_memory_passes() -> None:
    text = (
        "The frobnicator service must run its schema migration before the web "
        "tier boots, or the boot probe fails. Run the migrate step first."
    )
    assert secret_scan.scan(text) == []
    assert not secret_scan.contains_secret(text)


def test_scan_returns_only_class_names_never_content() -> None:
    secret = "password=" + "hunter2SuperSecretValue"
    hits = secret_scan.scan(secret)
    # The report carries labels only — the secret substring is never returned.
    assert hits
    assert all("hunter2" not in label for label in hits)


def test_prose_numbers_are_not_high_entropy() -> None:
    # A long run of digits (an id, a timestamp) is low entropy — not a secret.
    text = "processed 12345678901234567890 rows in the batch"
    assert secret_scan.HIGH_ENTROPY_CLASS not in secret_scan.scan(text)
