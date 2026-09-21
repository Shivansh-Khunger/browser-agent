"""Async nodriver browser session with deterministic owned-process teardown."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ..state.adapter import BrowserStateAdapter
from ..state.capture import observation_digest, redact_action_input
from ..state.models import (
    ActionEvidence,
    ActionRequest,
    CapturePolicy,
    CheckpointRef,
    CleanShutdownProof,
    DiagnosticRef,
    EpisodeLease,
    EpisodeMetadata,
    EpisodeOutcome,
    StateDelta,
)
from .models import (
    ActionResult,
    BrowserAction,
    BrowserConfig,
    BrowserDisconnectedError,
    BrowserEvidence,
    BrowserExitedError,
    BrowserMetadata,
    BrowserShutdownError,
    BrowserTimeoutError,
    EvidenceWindow,
    Observation,
    OutcomeStatus,
    SessionLifecycle,
    SessionStateError,
)
from .nodriver_runtime import NodriverLauncher
from .runtime import BrowserLauncher, OwnedBrowser, RuntimeFailureKind


class _EpisodeStateAdapter(BrowserStateAdapter, Protocol):
    """Internal browser/state bridge; writable paths never reach harness models."""

    def _profile_directory(self, lease: EpisodeLease) -> Path: ...


class NodriverSession:
    """Own one Chrome process and one state-adapter episode."""

    def __init__(
        self,
        config: BrowserConfig,
        state: _EpisodeStateAdapter,
        policy: CapturePolicy,
        episode_metadata: EpisodeMetadata,
        *,
        seed_checkpoint: CheckpointRef | None = None,
        launcher: BrowserLauncher | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._policy = policy
        self._episode_metadata = episode_metadata
        self._seed_checkpoint = seed_checkpoint
        self._launcher = launcher or NodriverLauncher()
        self._lifecycle = SessionLifecycle.NEW
        self._metadata: BrowserMetadata | None = None
        self._lease: EpisodeLease | None = None
        self._runtime: OwnedBrowser | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._terminal_checkpoint: CheckpointRef | None = None
        self._diagnostic: DiagnosticRef | None = None
        self._fatal_error: BaseException | None = None
        self._last_observation: Observation | None = None
        self._last_state_delta: StateDelta | None = None
        self._observability_failed = False
        self._action_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()

    @property
    def config(self) -> BrowserConfig:
        return self._config

    @property
    def lifecycle(self) -> SessionLifecycle:
        return self._lifecycle

    @property
    def active_target_id(self) -> str | None:
        return self._runtime.active_target_id if self._runtime else None

    @property
    def metadata(self) -> BrowserMetadata | None:
        return self._metadata

    @property
    def terminal_checkpoint(self) -> CheckpointRef | None:
        return self._terminal_checkpoint

    @property
    def diagnostic(self) -> DiagnosticRef | None:
        return self._diagnostic

    @property
    def fatal_error(self) -> BaseException | None:
        return self._fatal_error

    @property
    def last_state_delta(self) -> StateDelta | None:
        return self._last_state_delta

    @property
    def observability_failed(self) -> bool:
        return self._observability_failed

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._lifecycle is not SessionLifecycle.NEW:
                raise SessionStateError(f"cannot start from {self._lifecycle}")
            try:
                self._metadata = await self._launcher.inspect(self._config)
                state_metadata = replace(
                    self._episode_metadata,
                    browser_executable=str(self._metadata.executable),
                    browser_version=self._metadata.browser_version,
                    nodriver_version=self._metadata.nodriver_version,
                    config_digest=self._metadata.config_digest,
                    platform=self._metadata.platform,
                    locale_override=self._config.locale,
                    timezone_override=self._config.timezone,
                )
                self._lease = await self._state.open_episode(
                    self._seed_checkpoint, self._policy, state_metadata
                )
                profile = self._state._profile_directory(  # pyright: ignore[reportPrivateUsage]
                    self._lease
                )
                self._runtime = await self._launcher.launch(self._config, profile, self._metadata)
            except BaseException as error:
                try:
                    await asyncio.shield(self._cleanup_failed_start(error))
                finally:
                    self._lifecycle = SessionLifecycle.CLOSED
                raise
            self._lifecycle = SessionLifecycle.RUNNING
            self._monitor = asyncio.create_task(
                self._monitor_runtime(), name=f"browser-{self._lease.episode_id}"
            )

    async def observe(self) -> Observation:
        runtime = self._require_running()
        async with self._action_lock:
            self._require_running()
            try:
                async with asyncio.timeout(self._config.timeouts.action):
                    observation = await runtime.observe()
                    self._last_observation = observation
                    return observation
            except TimeoutError as error:
                raise BrowserTimeoutError("browser observation timed out") from error

    async def execute(self, action: BrowserAction) -> ActionResult:
        runtime = self._require_running()
        async with self._action_lock:
            self._require_running()
            lease = self._lease
            if lease is None:
                raise SessionStateError("session has no episode lease")
            previous = self._last_observation
            capture = await self._state.begin_action(
                lease,
                ActionRequest(
                    task_id=self._episode_metadata.task_id or "session",
                    action_id=uuid4().hex,
                    name=action.name,
                    redacted_input=redact_action_input(action),
                    target=action.target,
                    pre_observation_id=previous.observation_id if previous else None,
                    pre_observation_digest=observation_digest(previous),
                    pre_target_id=previous.active_target_id if previous else self.active_target_id,
                    pre_url=previous.url if previous else None,
                    read_only=action.read_only,
                ),
            )
            evidence_window = await self._begin_evidence(runtime)
            try:
                async with asyncio.timeout(self._config.timeouts.action):
                    result = await runtime.execute(action)
            except TimeoutError:
                # Mutation might already have reached Chromium when CDP or page
                # settlement runs out. Return an explicit non-retryable outcome
                # instead of inviting the harness to replay a click or form fill.
                status = OutcomeStatus.FAILED if action.read_only else OutcomeStatus.UNCERTAIN
                result = ActionResult(
                    status,
                    f"browser action {action.name} timed out",
                    error_code="timeout",
                    retryable=False,
                )
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                failed = ActionResult(
                    OutcomeStatus.FAILED,
                    f"browser action {action.name} raised {type(error).__name__}",
                    error_code=getattr(error, "code", "action_exception"),
                    retryable=bool(getattr(error, "retryable", False)),
                )
                evidence = await self._finish_evidence(runtime, evidence_window)
                with suppress(Exception):
                    self._last_state_delta = await self._state.finish_action(
                        capture, failed, previous, ActionEvidence(evidence)
                    )
                raise

            observation = previous if action.read_only else result.observation
            if result.observation is not None:
                self._last_observation = result.observation
                observation = result.observation
            evidence = await self._finish_evidence(runtime, evidence_window)
            if evidence.required_failure is not None:
                self._observability_failed = True
                result = self._capture_failure_result(action, result, evidence.required_failure)
            try:
                self._last_state_delta = await self._state.finish_action(
                    capture, result, observation, ActionEvidence(evidence)
                )
            except Exception as error:
                self._observability_failed = True
                return self._capture_failure_result(action, result, type(error).__name__)
            return result

    @staticmethod
    def _capture_failure_result(
        action: BrowserAction, result: ActionResult, error: str
    ) -> ActionResult:
        original_status = result.details.get("action_status", result.status.value)
        status = (
            OutcomeStatus.UNCERTAIN
            if not action.read_only and original_status != OutcomeStatus.FAILED.value
            else OutcomeStatus.PARTIAL
        )
        return replace(
            result,
            status=status,
            message=(
                result.message
                if result.error_code == "capture_failed"
                else f"{result.message}; required capture failed"
            ),
            error_code="capture_failed",
            retryable=False,
            details={
                **result.details,
                "capture_error": error,
                "action_status": original_status,
            },
        )

    @staticmethod
    async def _begin_evidence(runtime: OwnedBrowser) -> EvidenceWindow:
        method = getattr(runtime, "begin_evidence", None)
        if method is None:
            return EvidenceWindow(0, 0)
        try:
            return await method()
        except Exception as error:
            return EvidenceWindow(
                0,
                0,
                required_errors=(f"browser_evidence_begin:{type(error).__name__}",),
            )

    @staticmethod
    async def _finish_evidence(runtime: OwnedBrowser, window: EvidenceWindow) -> BrowserEvidence:
        method = getattr(runtime, "finish_evidence", None)
        if method is None:
            return BrowserEvidence(omissions=("browser_evidence_unavailable",))
        try:
            evidence = await method(window)
            if window.required_errors and evidence.required_failure is None:
                return replace(evidence, required_failure=window.required_errors[0])
            return evidence
        except Exception as error:
            return BrowserEvidence(
                warnings=(f"browser_evidence_failed:{type(error).__name__}",),
                omissions=("required_browser_evidence",),
                required_failure=f"browser_evidence_finish:{type(error).__name__}",
            )

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(self._close_once())
            close_task = self._close_task
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await close_task
            raise

    def _require_running(self) -> OwnedBrowser:
        if self._lifecycle is not SessionLifecycle.RUNNING or self._runtime is None:
            if self._fatal_error is not None:
                raise SessionStateError(f"session failed: {type(self._fatal_error).__name__}")
            raise SessionStateError(f"session is {self._lifecycle}")
        return self._runtime

    async def _close_once(self) -> None:
        if self._lifecycle is SessionLifecycle.CLOSED:
            if self._fatal_error is not None:
                raise self._fatal_error
            return
        if self._lifecycle is SessionLifecycle.NEW:
            self._lifecycle = SessionLifecycle.CLOSED
            return
        if self._lifecycle is SessionLifecycle.CLOSING and self._monitor is not None:
            with suppress(Exception):
                await asyncio.shield(self._monitor)
            if self._fatal_error is not None:
                raise self._fatal_error
            return
        self._lifecycle = SessionLifecycle.CLOSING
        await self._cancel_monitor()
        runtime = self._runtime
        lease = self._lease
        if runtime is None or lease is None:
            self._lifecycle = SessionLifecycle.CLOSED
            return

        try:
            async with asyncio.timeout(self._config.timeouts.shutdown):
                async with self._action_lock:
                    await runtime.request_close()
                    exit_code = await asyncio.shield(runtime.wait_for_exit())
                    await runtime.close_connection()
            if exit_code != 0:
                raise BrowserExitedError(f"Chrome exited with status {exit_code}")
            async with asyncio.timeout(self._config.timeouts.shutdown):
                await self._state.confirm_shutdown(
                    lease,
                    CleanShutdownProof(
                        episode_id=lease.episode_id,
                        process_id=runtime.process_id,
                        exit_code=exit_code,
                    ),
                )
                self._terminal_checkpoint = await self._state.close_episode(
                    lease, EpisodeOutcome.SUCCEEDED
                )
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            terminal_error: BaseException = (
                BrowserShutdownError("Chrome shutdown timed out")
                if isinstance(error, TimeoutError)
                else error
            )
            try:
                await self._force_stop(runtime)
            except BrowserShutdownError as cleanup_error:
                terminal_error = cleanup_error
            else:
                await self._abort_episode(lease, terminal_error)
            self._fatal_error = terminal_error
            raise terminal_error from error
        finally:
            self._lifecycle = SessionLifecycle.CLOSED

    async def _monitor_runtime(self) -> None:
        runtime = self._runtime
        lease = self._lease
        if runtime is None or lease is None:
            return
        try:
            failure = await runtime.wait_for_failure()
        except asyncio.CancelledError:
            return
        async with self._lifecycle_lock:
            if self._lifecycle is not SessionLifecycle.RUNNING:
                return
            self._lifecycle = SessionLifecycle.CLOSING
        if failure.kind is RuntimeFailureKind.DISCONNECTED:
            error: BaseException = BrowserDisconnectedError("Chrome DevTools connection closed")
            try:
                await self._force_stop(runtime)
            except BrowserShutdownError as cleanup_error:
                error = cleanup_error
                cleanup_verified = False
            else:
                cleanup_verified = True
        else:
            error = BrowserExitedError(
                f"Chrome exited unexpectedly with status {failure.exit_code}"
            )
            cleanup_verified = True
            try:
                async with asyncio.timeout(self._config.timeouts.shutdown):
                    await runtime.close_connection()
            except Exception:
                error = BrowserShutdownError(
                    "Chrome exited but DevTools connection cleanup timed out"
                )
        if cleanup_verified:
            await self._abort_episode(lease, error)
        self._fatal_error = error
        self._lifecycle = SessionLifecycle.CLOSED

    async def _cleanup_failed_start(self, error: BaseException) -> None:
        cleanup_verified = not isinstance(error, BrowserShutdownError)
        if self._runtime is not None:
            try:
                await self._force_stop(self._runtime)
            except BrowserShutdownError as cleanup_error:
                error = cleanup_error
                cleanup_verified = False
        if self._lease is not None and cleanup_verified:
            await self._abort_episode(self._lease, error)
        self._fatal_error = error
        if not cleanup_verified:
            raise error

    async def _abort_episode(self, lease: EpisodeLease, error: BaseException) -> None:
        with suppress(Exception):
            async with asyncio.timeout(self._config.timeouts.shutdown):
                self._diagnostic = await self._state.abort_episode(lease, error)

    async def _force_stop(self, runtime: OwnedBrowser) -> None:
        # Graceful shutdown obeys configured deadline. Forced process reaping gets
        # its own fixed safety budget so tiny test/operator deadlines cannot leave
        # Chrome alive with profile locks held.
        await runtime.force_stop(1.0)

    async def _cancel_monitor(self) -> None:
        monitor = self._monitor
        if monitor is None or monitor.done() or monitor is asyncio.current_task():
            return
        monitor.cancel()
        with suppress(asyncio.CancelledError):
            await monitor
