from __future__ import annotations

from typing import cast

import pytest
from nodriver import cdp
from nodriver.core.tab import Tab

from browser_agent.browser.nodriver_frames import (
    FrameContext,
    FrameRegistry,
    _safe_origin,
    frame_generations,
)


def test_safe_origin_removes_credentials_path_query_and_fragment() -> None:
    assert (
        _safe_origin("https://user:secret@example.test:8443/path?token=canary#fragment")
        == "https://example.test:8443"
    )


def test_safe_origin_formats_ipv6_and_rejects_opaque_or_invalid_ports() -> None:
    assert _safe_origin("http://[::1]:8080/page") == "http://[::1]:8080"
    assert _safe_origin("data:text/plain,hello") is None
    assert _safe_origin("https://example.test:not-a-port/path") is None


def test_breadcrumb_rebase_updates_synthetic_descendants() -> None:
    session = cast(Tab, object())
    grandchild = FrameContext(
        cdp.page.FrameId("grandchild"),
        session,
        1,
        ("main", "frame-1", "frame-1"),
        "https://grandchild.test",
        (),
        True,
    )
    child = FrameContext(
        cdp.page.FrameId("child"),
        session,
        1,
        ("main", "frame-1"),
        "https://child.test",
        (grandchild,),
        True,
    )

    rebased = child.with_breadcrumb(("main", "checkout"))

    assert rebased.breadcrumb == ("main", "checkout")
    assert rebased.children[0].breadcrumb == ("main", "checkout", "frame-1")
    assert frame_generations(rebased) == {"child": 1, "grandchild": 1}


class _ClosingSession:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        if self.error:
            raise self.error


@pytest.mark.asyncio
async def test_registry_close_attempts_every_frame_session() -> None:
    registry = FrameRegistry(cast(Tab, object()))
    failed = _ClosingSession(RuntimeError("close failed"))
    closed = _ClosingSession()
    registry._oopif_sessions = {  # pyright: ignore[reportPrivateUsage]
        "failed": cast(object, failed),
        "closed": cast(object, closed),
    }

    with pytest.raises(ExceptionGroup, match="failed to close frame sessions"):
        await registry.close()

    assert failed.closed is True
    assert closed.closed is True
    assert list(registry._oopif_sessions) == ["failed"]  # pyright: ignore[reportPrivateUsage]
