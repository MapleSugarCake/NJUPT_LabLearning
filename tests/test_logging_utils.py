from labpass.logging_utils import redact_text, safe_excerpt


def test_redacts_common_secrets_and_student_number() -> None:
    raw = (
        'username="202500001" password=secret token:abc123 '
        "Authorization=Bearer.secret ticket=ST-123 JSESSIONID=session-value"
    )

    redacted = redact_text(raw)

    assert "202500001" not in redacted
    assert "secret" not in redacted
    assert "abc123" not in redacted
    assert "ST-123" not in redacted
    assert "session-value" not in redacted


def test_excerpt_is_single_line_and_bounded() -> None:
    excerpt = safe_excerpt("a\n" * 400, limit=30)
    assert "\n" not in excerpt
    assert len(excerpt) == 31
    assert excerpt.endswith("…")
