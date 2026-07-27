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
        # the query goes whole, keys included: a token is as often a bare key as
        # it is a value, and no rule tells an auth parameter from a format one
        ("https://example.com/feed?s3cret", f"https://example.com/feed?{REDACTED}"),
        ("https://example.com/feed?s3cret=", f"https://example.com/feed?{REDACTED}"),
        (
            "https://user:pw@example.com/feed?a=1&b=2",
            f"https://{REDACTED}@example.com/feed?{REDACTED}",
        ),
    ],
)
def test_redact_url(url: str, expected: str) -> None:
    """Every part of a URL that can carry a secret is masked."""
    assert redact_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/rss?s3cret",
        "https://example.com/rss?token=s3cret",
        "https://example.com/rss?s3cret=on&s3cret=off",
        "https://s3cret@example.com/rss",
        "https://user:s3cret@example.com/rss",
        "https://example.com/rss#s3cret",
    ],
)
def test_no_url_shape_leaks_its_secret(url: str) -> None:
    """A secret must not survive masking, wherever in the URL it sits.

    The named parts are covered above; this pins the property itself, so a future
    change to the masking rule cannot quietly let one shape through.
    """
    assert "s3cret" not in redact_url(url)
    assert "s3cret" not in redact_urls(f"cannot reach {url} right now")


def test_redact_urls_masks_any_scheme_quoted_in_text() -> None:
    """URLs inside a message are masked whatever scheme the transport used."""
    text = "cannot reach 'feed+ftp://user:pw@example.com/feed?token=t0ken' twice"

    masked = redact_urls(text)

    assert "pw" not in masked
    assert "t0ken" not in masked
    assert masked == (
        f"cannot reach 'feed+ftp://{REDACTED}@example.com/feed?{REDACTED}' twice"
    )
