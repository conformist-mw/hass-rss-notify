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


@pytest.mark.parametrize(
    "url",
    [
        "mailto:s3cret@example.com",
        "mailto:?s3cret",
        "data:text/html,x#s3cret",
    ],
)
def test_redact_urls_masks_an_opaque_url_that_can_carry_a_secret(url: str) -> None:
    """An opaque URL has no host to keep, so it is replaced whole inside a message.

    `scheme://host/path` is not the only shape a URL field accepts. An opaque one
    carrying an `@`, a `?` or a `#` is masked too, because there is nowhere in it
    a secret could not sit.
    """
    masked = redact_urls(f"Error fetching feed from {url}: boom")

    assert "s3cret" not in masked
    assert REDACTED in masked


@pytest.mark.parametrize(
    "text",
    [
        # aiohttp's connection errors: `scheme:word` shapes that are not URLs.
        # A pattern loose enough to catch `urn:x` swallows these, and masking the
        # host and port of a failure would make every report useless.
        "Cannot connect to host example.com:443 ssl:default [Connect call failed]",
        "Cannot connect to host example.com:443 ssl:True [nodename nor servname]",
        "Timeout on reading data from socket, host example.com:8080",
    ],
)
def test_redact_urls_leaves_a_hostport_diagnosis_intact(text: str) -> None:
    """Masking must not eat the host and port aiohttp reports a failure against."""
    assert redact_urls(text) == text
