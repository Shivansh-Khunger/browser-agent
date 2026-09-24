from __future__ import annotations

from browser_agent.browser.nodriver_actions import _domain_allowed, _normalize_url


def test_normalize_url_adds_scheme_to_bare_host() -> None:
    assert _normalize_url("example.test/path") == "https://example.test/path"


def test_normalize_url_keeps_explicit_scheme() -> None:
    assert _normalize_url("http://example.test") == "http://example.test"


def test_normalize_url_rejects_blank_or_hostless_input() -> None:
    assert _normalize_url("") is None
    assert _normalize_url("   ") is None
    assert _normalize_url("https:///no-host") is None


def test_normalize_url_rejects_backslash_host_confusion() -> None:
    # Chrome's WHATWG URL parser folds '\\' into '/' in the authority, resolving
    # this to host "evil.test" even though urlparse reads host "allowed.test" -
    # exactly the divergence the allowlist check below must not be fooled by.
    assert _normalize_url(r"https://evil.test\@allowed.test/") is None


def test_domain_allowed_with_empty_allowlist_is_unrestricted() -> None:
    assert _domain_allowed("https://anything.test", ()) is True


def test_domain_allowed_matches_exact_and_subdomain() -> None:
    allowed = ("example.test",)
    assert _domain_allowed("https://example.test/page", allowed) is True
    assert _domain_allowed("https://sub.example.test", allowed) is True
    assert _domain_allowed("https://notexample.test", allowed) is False
    assert _domain_allowed("https://evil.test", allowed) is False
