"""Tests for the URL masking shared by diagnostics, the client and the flow."""

from homeassistant.helpers.redact import REDACTED
import pytest

from custom_components.rss_notify.redact import redact_url, redact_urls


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("", ""),
        # nothing that can be masked field by field is passed through at all
        ("http://[::1", REDACTED),
        ("mailto:feeds@example.com", REDACTED),
        ("https://example.com:8080/feed.xml", "https://example.com:8080/feed.xml"),
        ("https://example.com/feed#s3cret", "https://example.com/feed"),
        ("https://example.com/feed?bare", f"https://example.com/feed?bare={REDACTED}"),
        (
            "https://user:pw@example.com/feed?a=1&b=2",
            f"https://{REDACTED}@example.com/feed?a={REDACTED}&b={REDACTED}",
        ),
    ],
)
def test_redact_url(url: str, expected: str) -> None:
    """Every part of a URL that can carry a secret is masked."""
    assert redact_url(url) == expected


def test_redact_urls_masks_any_scheme_quoted_in_text() -> None:
    """URLs inside a message are masked whatever scheme the transport used."""
    text = "cannot reach 'feed+ftp://user:pw@example.com/feed?token=t0ken' twice"

    masked = redact_urls(text)

    assert "pw" not in masked
    assert "t0ken" not in masked
    assert masked == (
        f"cannot reach 'feed+ftp://{REDACTED}@example.com/feed?token={REDACTED}' twice"
    )
