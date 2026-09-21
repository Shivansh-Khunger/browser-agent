from __future__ import annotations

import asyncio
import os
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
    OutcomeStatus,
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


class UnstoppableBrowser(FakeOwnedBrowser):
    async def force_stop(self, timeout: float) -> None:
        del timeout
        raise BrowserShutdownError("Chrome process exit could not be verified")


class EvidenceFailBrowser(FakeOwnedBrowser):
    async def finish_evidence(self, window):  # type: ignore[no-untyped-def]
        del window
        raise OSError("injected evidence failure")


class NoGracefulCloseBrowser:
    def __init__(self, runtime) -> None:  # type: ignore[no-untyped-def]
        self._runtime = runtime

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        return getattr(self._runtime, name)

    async def request_close(self) -> None:
        return None


class PausedCloseBrowser(NoGracefulCloseBrowser):
    def __init__(self, runtime) -> None:  # type: ignore[no-untyped-def]
        super().__init__(runtime)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def request_close(self) -> None:
        self.entered.set()
        await self.release.wait()
        await self._runtime.request_close()


class WrappingNodriverLauncher(NodriverLauncher):
    def __init__(self, wrapper_type) -> None:  # type: ignore[no-untyped-def]
        self.wrapper_type = wrapper_type
        self.wrapper = None

    async def launch(self, config, profile, metadata):  # type: ignore[no-untyped-def]
        runtime = await super().launch(config, profile, metadata)
        self.wrapper = self.wrapper_type(runtime)
        return self.wrapper


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


class FailingCaptureState(ProfileStateAdapter):
    async def finish_action(self, capture, result, observation, evidence=None):  # type: ignore[no-untyped-def]
        del capture, result, observation, evidence
        raise OSError("injected capture failure")


class SequenceLauncher(FakeBrowserLauncher):
    def __init__(self, *runtimes: FakeOwnedBrowser, fail_launch: int | None = None) -> None:
        super().__init__(runtimes[0])
        self.runtimes = list(runtimes)
        self.fail_launch = fail_launch
        self.launch_count = 0
        self.launched_profiles: list[Path] = []

    async def launch(self, config, profile, metadata):  # type: ignore[no-untyped-def]
        del config, metadata
        self.launched_profiles.append(profile)
        index = self.launch_count
        self.launch_count += 1
        if self.fail_launch == index:
            raise RuntimeError("injected rollover launch failure")
        return self.runtimes[index]


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
    assert [request.name for request in state.begun_actions] == ["fixture-action"]
    assert len(state.finished_actions) == 1

    with pytest.raises(SessionStateError, match="cannot start from closed"):
        await session.start()


@pytest.mark.asyncio
async def test_explicit_checkpoint_rolls_over_and_preserves_lineage(tmp_path) -> None:
    state = ProfileStateAdapter(tmp_path)
    first_runtime = FakeOwnedBrowser()
    second_runtime = FakeOwnedBrowser()
    launcher = SequenceLauncher(first_runtime, second_runtime)
    session = NodriverSession(
        BrowserConfig(), state, policy(), episode_metadata(), launcher=launcher
    )
    await session.start()

    checkpoint = await session.checkpoint("operator-request")

    assert session.lifecycle is SessionLifecycle.RUNNING
    assert session.restore_authority == checkpoint
    assert first_runtime.close_requests == 1
    assert first_runtime.connection_closes == 1
    assert len(launcher.launched_profiles) == 2
    await session.execute(BrowserAction("after-rollover"))
    await session.close()
    assert session.terminal_checkpoint is not None
    assert session.terminal_checkpoint.parent_id == checkpoint.checkpoint_id
    assert session.restore_authority == session.terminal_checkpoint
    assert second_runtime.close_requests == 1


@pytest.mark.asyncio
async def test_rollover_relaunch_failure_retains_last_valid_restore_authority(tmp_path) -> None:
    state = ProfileStateAdapter(tmp_path)
    first_runtime = FakeOwnedBrowser()
    launcher = SequenceLauncher(first_runtime, fail_launch=1)
    session = NodriverSession(
        BrowserConfig(), state, policy(), episode_metadata(), launcher=launcher
    )
    await session.start()

    with pytest.raises(RuntimeError, match="injected rollover launch failure"):
        await session.checkpoint("before-failure")

    assert session.lifecycle is SessionLifecycle.CLOSED
    assert session.restore_authority is not None
    assert session.restore_authority.reason == "before-failure"
    assert len(state.aborted_with) == 1


@pytest.mark.asyncio
async def test_session_redacts_input_before_state_adapter_boundary(tmp_path) -> None:
    state = ProfileStateAdapter(tmp_path)
    session = NodriverSession(
        BrowserConfig(),
        state,
        policy(),
        episode_metadata(),
        launcher=FakeBrowserLauncher(FakeOwnedBrowser()),
    )
    await session.start()

    await session.execute(BrowserAction("type", {"text": "secret-canary"}))
    await session.close()

    request = state.begun_actions[0]
    assert "secret-canary" not in repr(request.redacted_input)
    assert request.redacted_input["text"] == {
        "placeholder": "[REDACTED]",
        "character_count": 13,
        "sensitivity": "type",
    }


@pytest.mark.asyncio
async def test_required_capture_failure_marks_mutation_uncertain(tmp_path) -> None:
    state = FailingCaptureState(tmp_path)
    session = NodriverSession(
        BrowserConfig(),
        state,
        policy(),
        episode_metadata(),
        launcher=FakeBrowserLauncher(FakeOwnedBrowser()),
    )
    await session.start()

    result = await session.execute(BrowserAction("click"))
    await session.close()

    assert result.status is OutcomeStatus.UNCERTAIN
    assert result.error_code == "capture_failed"
    assert result.details["action_status"] == "succeeded"
    assert session.observability_failed is True


@pytest.mark.asyncio
async def test_required_browser_evidence_failure_is_recorded_and_marks_mutation_uncertain(
    tmp_path,
) -> None:
    state = ProfileStateAdapter(tmp_path)
    session = NodriverSession(
        BrowserConfig(),
        state,
        policy(),
        episode_metadata(),
        launcher=FakeBrowserLauncher(EvidenceFailBrowser()),
    )
    await session.start()

    result = await session.execute(BrowserAction("click"))
    await session.close()

    assert result.status is OutcomeStatus.UNCERTAIN
    assert result.error_code == "capture_failed"
    assert state.finished_actions[0][1].error_code == "capture_failed"
    assert session.observability_failed is True


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
    with pytest.raises(BrowserExitedError, match="status 7"):
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
    with pytest.raises(BrowserDisconnectedError):
        await session.close()


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
async def test_unverified_forced_exit_is_fatal_and_does_not_remove_live_profile(
    tmp_path,
) -> None:
    state = ProfileStateAdapter(tmp_path)
    runtime = UnstoppableBrowser(close_exit_code=None)
    session = NodriverSession(
        BrowserConfig(),
        state,
        policy(),
        episode_metadata(),
        launcher=FakeBrowserLauncher(runtime),
    )
    await session.start()
    profile = state._profile_directory(session._lease)  # type: ignore[arg-type]

    runtime.disconnect()
    await _wait_until_closed(session)

    with pytest.raises(BrowserShutdownError, match="could not be verified"):
        await session.close()
    assert profile.exists()
    assert session.diagnostic is None
    assert state.aborted_with == []


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
async def test_timed_out_version_probe_is_killed_and_reaped(tmp_path) -> None:
    executable = tmp_path / "hanging-chromium"
    pid_file = tmp_path / "version.pid"
    executable.write_text(
        f"#!/bin/sh\necho $$ > {str(pid_file)!r}\ntrap '' TERM\nwhile :; do :; done\n"
    )
    executable.chmod(0o700)

    with pytest.raises(Exception, match="inspect browser version"):
        await NodriverLauncher().inspect(
            BrowserConfig(
                executable_path=executable,
                timeouts=TimeoutConfig(launch=0.5),
            )
        )

    process_id = int(pid_file.read_text())
    assert not _process_exists(process_id)


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
    await session.close()

    assert session.terminal_checkpoint is not None
    assert not profile.exists()
    assert not _process_exists(process_id)


@pytest.mark.asyncio
async def test_installed_browser_unexpected_exit_leaks_no_process_or_profile_lock(
    tmp_path,
) -> None:
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
    runtime = session._runtime
    assert runtime is not None
    process_id = runtime.process_id
    runtime.terminate()  # type: ignore[attr-defined]
    await _wait_until_closed(session)

    with pytest.raises((BrowserExitedError, BrowserDisconnectedError)):
        await session.close()
    assert session.terminal_checkpoint is None
    assert session.diagnostic is not None
    assert not profile.exists()
    assert not _process_exists(process_id)


@pytest.mark.asyncio
async def test_installed_browser_disconnect_leaks_no_process_or_profile_lock(
    tmp_path,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    state = LocalBrowserStateAdapter(tmp_path / "state", store)
    session = NodriverSession(BrowserConfig(headless=True), state, policy(), episode_metadata())
    await _start_or_skip(session)

    profile = state._profile_directory(session._lease)  # type: ignore[arg-type]
    runtime = session._runtime
    assert runtime is not None
    process_id = runtime.process_id
    browser = runtime._browser  # type: ignore[attr-defined]
    assert browser.socket is not None
    await browser.socket.close()
    await _wait_until_closed(session)

    with pytest.raises(BrowserDisconnectedError):
        await session.close()
    assert session.terminal_checkpoint is None
    assert session.diagnostic is not None
    assert not profile.exists()
    assert not _process_exists(process_id)


@pytest.mark.asyncio
async def test_installed_browser_forced_shutdown_leaks_no_process_or_profile_lock(
    tmp_path,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    state = LocalBrowserStateAdapter(tmp_path / "state", store)
    launcher = WrappingNodriverLauncher(NoGracefulCloseBrowser)
    session = NodriverSession(
        BrowserConfig(headless=True, timeouts=TimeoutConfig(shutdown=0.05)),
        state,
        policy(),
        episode_metadata(),
        launcher=launcher,
    )
    await _start_or_skip(session)

    profile = state._profile_directory(session._lease)  # type: ignore[arg-type]
    process_id = session._runtime.process_id  # type: ignore[union-attr]
    with pytest.raises(BrowserShutdownError, match="timed out"):
        await session.close()

    assert not profile.exists()
    assert not _process_exists(process_id)


@pytest.mark.asyncio
async def test_installed_browser_cancelled_close_still_releases_process_and_lock(
    tmp_path,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    state = LocalBrowserStateAdapter(tmp_path / "state", store)
    launcher = WrappingNodriverLauncher(PausedCloseBrowser)
    session = NodriverSession(
        BrowserConfig(headless=True),
        state,
        policy(),
        episode_metadata(),
        launcher=launcher,
    )
    await _start_or_skip(session)
    wrapper = launcher.wrapper
    assert isinstance(wrapper, PausedCloseBrowser)
    profile = state._profile_directory(session._lease)  # type: ignore[arg-type]
    process_id = session._runtime.process_id  # type: ignore[union-attr]

    closing = asyncio.create_task(session.close())
    await wrapper.entered.wait()
    closing.cancel()
    wrapper.release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert session.terminal_checkpoint is not None
    assert not profile.exists()
    assert not _process_exists(process_id)


async def _wait_until_closed(session: NodriverSession) -> None:
    async with asyncio.timeout(1):
        while session.lifecycle is not SessionLifecycle.CLOSED:
            await asyncio.sleep(0)


async def _start_or_skip(session: NodriverSession) -> None:
    try:
        await session.start()
    except Exception as error:
        if "No installed Chrome or Chromium" in str(error):
            pytest.skip(str(error))
        raise


def _process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    return True
