from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from browser_agent.browser import (
    BrowserAction,
    BrowserConfig,
    BrowserDisconnectedError,
    BrowserExitedError,
    BrowserMetadata,
    BrowserShutdownError,
    NodriverSession,
    SessionLifecycle,
    SessionStateError,
    TimeoutConfig,
)
from browser_agent.browser.nodriver_runtime import NodriverLauncher
from browser_agent.state import (
    CapturePolicy,
    EpisodeMetadata,
    LocalArtifactStore,
    LocalBrowserStateAdapter,
)
from tests.fakes import FakeBrowserLauncher, FakeBrowserStateAdapter, FakeOwnedBrowser


class SlowCloseBrowser(FakeOwnedBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.close_entered = asyncio.Event()
        self.release_close = asyncio.Event()

    async def request_close(self) -> None:
        self.close_entered.set()
        await self.release_close.wait()
        await super().request_close()


class ProfileStateAdapter(FakeBrowserStateAdapter):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.aborted_with: list[BaseException] = []

    async def open_episode(self, seed_checkpoint, policy, metadata):  # type: ignore[no-untyped-def]
        lease = await super().open_episode(seed_checkpoint, policy, metadata)
        self._profile_directory(lease).mkdir(parents=True)
        return lease

    def _profile_directory(self, lease):  # type: ignore[no-untyped-def]
        return self.root / lease.episode_id / "profile"

    async def abort_episode(self, lease, error):  # type: ignore[no-untyped-def]
        self.aborted_with.append(error)
        return await super().abort_episode(lease, error)


def policy() -> CapturePolicy:
    return CapturePolicy("v1", "redaction-v1", restricted_storage=True)


def episode_metadata() -> EpisodeMetadata:
    return EpisodeMetadata(code_revision="test", platform="replaced-at-launch")


@pytest.mark.asyncio
async def test_session_launches_owned_profile_records_metadata_and_closes_once(tmp_path) -> None:
    state = ProfileStateAdapter(tmp_path)
    runtime = FakeOwnedBrowser()
    launcher = FakeBrowserLauncher(runtime)
    session = NodriverSession(
        BrowserConfig(), state, policy(), episode_metadata(), launcher=launcher
    )

    await session.start()
    result = await session.execute(BrowserAction("fixture-action"))
    await asyncio.gather(session.close(), session.close())
    await session.close()

    assert result.message == "fixture-action completed"
    assert session.lifecycle is SessionLifecycle.CLOSED
    assert session.metadata == launcher.metadata
    assert session.terminal_checkpoint is not None
    assert launcher.launched_profile is not None
    assert launcher.launched_profile.name == "profile"
    stored_metadata = next(iter(state.metadata_by_episode.values()))
    assert stored_metadata.browser_executable == "/installed/chrome"
    assert stored_metadata.browser_version == "Chrome 140.0.0.0"
    assert stored_metadata.nodriver_version == "0.50.3"
    assert stored_metadata.config_digest == "sha256:config"
    assert stored_metadata.platform == "test-platform"
    assert runtime.close_requests == 1
    assert runtime.connection_closes == 1

    with pytest.raises(SessionStateError, match="cannot start from closed"):
        await session.start()


@pytest.mark.asyncio
async def test_partial_startup_failure_aborts_episode_and_cannot_retry(tmp_path) -> None:
    state = ProfileStateAdapter(tmp_path)
    runtime = FakeOwnedBrowser()
    launcher = FakeBrowserLauncher(runtime, launch_error=RuntimeError("partial launch"))
    session = NodriverSession(
        BrowserConfig(), state, policy(), episode_metadata(), launcher=launcher
    )

    with pytest.raises(RuntimeError, match="partial launch"):
        await session.start()

    assert session.lifecycle is SessionLifecycle.CLOSED
    assert session.diagnostic is not None
    assert len(state.aborted_with) == 1
    with pytest.raises(SessionStateError, match="cannot start from closed"):
        await session.start()


@pytest.mark.asyncio
async def test_unexpected_exit_is_fatal_and_aborts_episode(tmp_path) -> None:
    state = ProfileStateAdapter(tmp_path)
    runtime = FakeOwnedBrowser()
    session = NodriverSession(
        BrowserConfig(),
        state,
        policy(),
        episode_metadata(),
        launcher=FakeBrowserLauncher(runtime),
    )
    await session.start()

    runtime.fail_process(7)
    await _wait_until_closed(session)

    assert isinstance(session.fatal_error, BrowserExitedError)
    assert session.terminal_checkpoint is None
    assert session.diagnostic is not None
    with pytest.raises(SessionStateError, match="session failed"):
        await session.execute(BrowserAction("after-crash"))
    await session.close()


@pytest.mark.asyncio
async def test_disconnect_terminates_process_and_aborts_episode(tmp_path) -> None:
    state = ProfileStateAdapter(tmp_path)
    runtime = FakeOwnedBrowser(close_exit_code=None)
    session = NodriverSession(
        BrowserConfig(),
        state,
        policy(),
        episode_metadata(),
        launcher=FakeBrowserLauncher(runtime),
    )
    await session.start()

    runtime.disconnect()
    await _wait_until_closed(session)

    assert isinstance(session.fatal_error, BrowserDisconnectedError)
    assert runtime.terminate_calls == 1
    assert session.diagnostic is not None


@pytest.mark.asyncio
async def test_shutdown_timeout_forces_process_exit_and_is_fatal(tmp_path) -> None:
    state = ProfileStateAdapter(tmp_path)
    runtime = FakeOwnedBrowser(close_exit_code=None)
    session = NodriverSession(
        BrowserConfig(timeouts=TimeoutConfig(shutdown=0.01)),
        state,
        policy(),
        episode_metadata(),
        launcher=FakeBrowserLauncher(runtime),
    )
    await session.start()

    with pytest.raises(BrowserShutdownError, match="timed out"):
        await session.close()

    assert session.lifecycle is SessionLifecycle.CLOSED
    assert runtime.terminate_calls == 1
    assert session.terminal_checkpoint is None
    assert session.diagnostic is not None


@pytest.mark.asyncio
async def test_cancelled_close_finishes_shielded_cleanup(tmp_path) -> None:
    state = ProfileStateAdapter(tmp_path)
    runtime = SlowCloseBrowser()
    session = NodriverSession(
        BrowserConfig(),
        state,
        policy(),
        episode_metadata(),
        launcher=FakeBrowserLauncher(runtime),
    )
    await session.start()

    closing = asyncio.create_task(session.close())
    await runtime.close_entered.wait()
    closing.cancel()
    asyncio.get_running_loop().call_soon(runtime.release_close.set)

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert session.lifecycle is SessionLifecycle.CLOSED
    assert session.terminal_checkpoint is not None
    assert runtime.close_requests == 1


@pytest.mark.asyncio
async def test_explicit_executable_path_records_version_without_download(tmp_path) -> None:
    executable = tmp_path / "chromium"
    executable.write_text("#!/bin/sh\nprintf 'Chromium 140.0.0.0\\n'\n")
    executable.chmod(0o700)

    metadata = await NodriverLauncher().inspect(BrowserConfig(executable_path=executable))

    assert metadata == BrowserMetadata(
        executable=executable.resolve(),
        browser_version="Chromium 140.0.0.0",
        nodriver_version="0.50.3",
        platform=metadata.platform,
        config_digest=metadata.config_digest,
    )


@pytest.mark.asyncio
async def test_installed_browser_releases_profile_lock_after_clean_close(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    state = LocalBrowserStateAdapter(tmp_path / "state", store)
    session = NodriverSession(BrowserConfig(headless=True), state, policy(), episode_metadata())
    try:
        await session.start()
    except Exception as error:
        if "No installed Chrome or Chromium" in str(error):
            pytest.skip(str(error))
        raise

    profile = state._profile_directory(session._lease)  # type: ignore[arg-type]
    process_id = session._runtime.process_id  # type: ignore[union-attr]
    await session.close()

    assert session.terminal_checkpoint is not None
    assert not profile.exists()
    assert not _process_exists(process_id)


async def _wait_until_closed(session: NodriverSession) -> None:
    async with asyncio.timeout(1):
        while session.lifecycle is not SessionLifecycle.CLOSED:
            await asyncio.sleep(0)


def _process_exists(process_id: int) -> bool:
    try:
        import os

        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    return True
