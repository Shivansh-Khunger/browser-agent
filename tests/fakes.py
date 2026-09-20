from __future__ import annotations

import hashlib
from asyncio import Future, get_running_loop
from collections import deque
from pathlib import Path

from browser_agent.browser.models import (
    ActionResult,
    BrowserAction,
    BrowserConfig,
    BrowserMetadata,
    Observation,
    OutcomeStatus,
    SessionLifecycle,
    SessionStateError,
    StaleTargetError,
    Viewport,
)
from browser_agent.browser.runtime import RuntimeFailure, RuntimeFailureKind
from browser_agent.browser.transport import BrowserTransport
from browser_agent.state.models import (
    ActionCapture,
    ActionRequest,
    ArtifactKind,
    ArtifactNotFoundError,
    ArtifactRef,
    CapturePolicy,
    CheckpointRef,
    CleanShutdownProof,
    DiagnosticRef,
    EpisodeLease,
    EpisodeMetadata,
    EpisodeOutcome,
    LeaseClosedError,
    SecurityClass,
    StateDelta,
)


def observation(observation_id: str) -> Observation:
    return Observation(
        observation_id=observation_id,
        active_target_id="target-1",
        url="https://example.test",
        title="Example",
        document_generation=1,
        frame_generations={"main": 1},
        viewport=Viewport(1280, 900),
    )


def clean_shutdown(episode_id: str) -> CleanShutdownProof:
    return CleanShutdownProof(episode_id=episode_id, process_id=4242, exit_code=0)


class FakeBrowserTransport:
    """Deterministic executable transport for browser contract tests."""

    def __init__(
        self,
        *,
        observations: tuple[Observation, ...] = (),
        results: tuple[ActionResult, ...] = (),
        start_error: BaseException | None = None,
    ) -> None:
        self._active_target_id: str | None = None
        self._observation_id: str | None = None
        self._observations = deque(observations)
        self._results = deque(results)
        self._start_error = start_error
        self.actions: list[BrowserAction] = []

    @property
    def active_target_id(self) -> str | None:
        return self._active_target_id

    async def start(self, config: BrowserConfig) -> None:
        if self._start_error:
            raise self._start_error
        self._active_target_id = "launch-target"

    async def observe(self) -> Observation:
        if not self._observations:
            raise AssertionError("no fake observation queued")
        observation = self._observations.popleft()
        self._active_target_id = observation.active_target_id
        self._observation_id = observation.observation_id
        return observation

    async def execute(self, action: BrowserAction) -> ActionResult:
        if action.target and action.target.observation_id != self._observation_id:
            raise StaleTargetError(
                f"target belongs to observation {action.target.observation_id}; "
                f"current observation is {self._observation_id}"
            )
        self.actions.append(action)
        if self._results:
            return self._results.popleft()
        return ActionResult(OutcomeStatus.SUCCEEDED, f"{action.name} completed")

    async def close(self) -> None:
        self._active_target_id = None


class FakeOwnedBrowser:
    """Controllable owned process for lifecycle tests."""

    def __init__(self, *, close_exit_code: int | None = 0) -> None:
        loop = get_running_loop()
        self._exit: Future[int] = loop.create_future()
        self._failure: Future[RuntimeFailure] = loop.create_future()
        self._close_exit_code = close_exit_code
        self.process_id = 4242
        self.active_target_id: str | None = "launch-target"
        self.close_requests = 0
        self.connection_closes = 0
        self.terminate_calls = 0
        self.kill_calls = 0

    async def observe(self) -> Observation:
        return observation("runtime-observation")

    async def execute(self, action: BrowserAction) -> ActionResult:
        return ActionResult(OutcomeStatus.SUCCEEDED, f"{action.name} completed")

    async def wait_for_failure(self) -> RuntimeFailure:
        return await self._failure

    async def request_close(self) -> None:
        self.close_requests += 1
        if self._close_exit_code is not None and not self._exit.done():
            self._exit.set_result(self._close_exit_code)

    async def close_connection(self) -> None:
        self.connection_closes += 1

    async def wait_for_exit(self) -> int:
        return await self._exit

    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self._exit.done():
            self._exit.set_result(-15)

    def kill(self) -> None:
        self.kill_calls += 1
        if not self._exit.done():
            self._exit.set_result(-9)

    def fail_process(self, exit_code: int) -> None:
        if not self._exit.done():
            self._exit.set_result(exit_code)
        if not self._failure.done():
            self._failure.set_result(RuntimeFailure(RuntimeFailureKind.PROCESS_EXIT, exit_code))

    def disconnect(self) -> None:
        if not self._failure.done():
            self._failure.set_result(RuntimeFailure(RuntimeFailureKind.DISCONNECTED))


class FakeBrowserLauncher:
    def __init__(
        self,
        runtime: FakeOwnedBrowser,
        *,
        inspect_error: BaseException | None = None,
        launch_error: BaseException | None = None,
    ) -> None:
        self.runtime = runtime
        self.inspect_error = inspect_error
        self.launch_error = launch_error
        self.launched_profile: Path | None = None
        self.metadata = BrowserMetadata(
            executable=Path("/installed/chrome"),
            browser_version="Chrome 140.0.0.0",
            nodriver_version="0.50.3",
            platform="test-platform",
            config_digest="sha256:config",
        )

    async def inspect(self, config: BrowserConfig) -> BrowserMetadata:
        del config
        if self.inspect_error:
            raise self.inspect_error
        return self.metadata

    async def launch(
        self, config: BrowserConfig, profile: Path, metadata: BrowserMetadata
    ) -> FakeOwnedBrowser:
        del config, metadata
        self.launched_profile = profile
        if self.launch_error:
            raise self.launch_error
        return self.runtime


class FakeBrowserSession:
    """Public session fake that exercises an injected browser transport."""

    def __init__(self, config: BrowserConfig, transport: BrowserTransport) -> None:
        self._config = config
        self._transport = transport
        self._lifecycle = SessionLifecycle.NEW

    @property
    def config(self) -> BrowserConfig:
        return self._config

    @property
    def lifecycle(self) -> SessionLifecycle:
        return self._lifecycle

    @property
    def active_target_id(self) -> str | None:
        return self._transport.active_target_id

    @property
    def metadata(self) -> BrowserMetadata | None:
        return None

    async def start(self) -> None:
        if self._lifecycle is not SessionLifecycle.NEW:
            raise SessionStateError(f"cannot start from {self._lifecycle}")
        try:
            await self._transport.start(self._config)
        except BaseException:
            self._lifecycle = SessionLifecycle.CLOSING
            await self._transport.close()
            self._lifecycle = SessionLifecycle.CLOSED
            raise
        self._lifecycle = SessionLifecycle.RUNNING

    def _require_running(self) -> None:
        if self._lifecycle is not SessionLifecycle.RUNNING:
            raise SessionStateError(f"session is {self._lifecycle}")

    async def observe(self) -> Observation:
        self._require_running()
        return await self._transport.observe()

    async def execute(self, action: BrowserAction) -> ActionResult:
        self._require_running()
        return await self._transport.execute(action)

    async def close(self) -> None:
        if self._lifecycle is SessionLifecycle.CLOSED:
            return
        self._lifecycle = SessionLifecycle.CLOSING
        await self._transport.close()
        self._lifecycle = SessionLifecycle.CLOSED


class FakeArtifactStore:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    async def put(
        self,
        data: bytes,
        *,
        kind: ArtifactKind,
        media_type: str,
        schema_version: int,
        security_class: SecurityClass,
        redaction_policy_version: str,
    ) -> ArtifactRef:
        digest = hashlib.sha256(data).hexdigest()
        content_id = f"sha256:{digest}"
        self._objects[content_id] = data
        return ArtifactRef(
            content_id=content_id,
            kind=kind,
            media_type=media_type,
            schema_version=schema_version,
            encoding="identity",
            compression=None,
            byte_length=len(data),
            security_class=security_class,
            redaction_policy_version=redaction_policy_version,
        )

    async def get(self, reference: ArtifactRef) -> bytes:
        try:
            return self._objects[reference.content_id]
        except KeyError as error:
            raise ArtifactNotFoundError(reference.content_id) from error


class FakeBrowserStateAdapter:
    """In-memory state adapter with stable IDs and no filesystem exposure."""

    def __init__(self) -> None:
        self._next_episode = 1
        self._next_capture = 1
        self._next_delta = 1
        self._next_checkpoint = 1
        self._open: dict[str, EpisodeLease] = {}
        self.policy_by_episode: dict[str, CapturePolicy] = {}
        self.metadata_by_episode: dict[str, EpisodeMetadata] = {}
        self._stopped: set[str] = set()

    def _lease(self, parent_checkpoint_id: str | None = None) -> EpisodeLease:
        number = self._next_episode
        self._next_episode += 1
        lease = EpisodeLease(f"lease-{number}", f"episode-{number}", parent_checkpoint_id)
        self._open[lease.lease_id] = lease
        return lease

    def _require_open(self, lease: EpisodeLease) -> None:
        if self._open.get(lease.lease_id) != lease:
            raise LeaseClosedError("episode lease is not open")

    def _require_episode_open(self, episode_id: str) -> None:
        if not any(lease.episode_id == episode_id for lease in self._open.values()):
            raise LeaseClosedError("episode is not open")

    async def open_episode(
        self,
        seed_checkpoint: CheckpointRef | None,
        policy: CapturePolicy,
        metadata: EpisodeMetadata,
    ) -> EpisodeLease:
        lease = self._lease(seed_checkpoint.checkpoint_id if seed_checkpoint else None)
        self.policy_by_episode[lease.episode_id] = policy
        self.metadata_by_episode[lease.episode_id] = metadata
        return lease

    async def begin_action(self, lease: EpisodeLease, request: ActionRequest) -> ActionCapture:
        self._require_open(lease)
        number = self._next_capture
        self._next_capture += 1
        return ActionCapture(f"capture-{number}", lease.episode_id, request)

    async def finish_action(
        self,
        capture: ActionCapture,
        result: ActionResult,
        observation: Observation | None,
    ) -> StateDelta:
        self._require_episode_open(capture.episode_id)
        number = self._next_delta
        self._next_delta += 1
        return StateDelta(
            delta_id=f"delta-{number}",
            episode_id=capture.episode_id,
            task_id=capture.request.task_id,
            action_id=capture.request.action_id,
            pre_observation_id=capture.request.pre_observation_id,
            post_observation_id=observation.observation_id if observation else None,
            outcome=result.status,
        )

    async def checkpoint(self, lease: EpisodeLease, reason: str) -> CheckpointRef:
        self._require_open(lease)
        if lease.episode_id not in self._stopped:
            raise LeaseClosedError("episode has no clean shutdown proof")
        checkpoint = self._checkpoint(lease, reason)
        del self._open[lease.lease_id]
        return checkpoint

    def _checkpoint(self, lease: EpisodeLease, reason: str) -> CheckpointRef:
        number = self._next_checkpoint
        self._next_checkpoint += 1
        return CheckpointRef(
            checkpoint_id=f"checkpoint-{number}",
            episode_id=lease.episode_id,
            parent_id=lease.parent_checkpoint_id,
            reason=reason,
            clean_shutdown=True,
        )

    async def close_episode(self, lease: EpisodeLease, outcome: EpisodeOutcome) -> CheckpointRef:
        self._require_open(lease)
        if lease.episode_id not in self._stopped:
            raise LeaseClosedError("episode has no clean shutdown proof")
        checkpoint = self._checkpoint(lease, f"terminal:{outcome}")
        del self._open[lease.lease_id]
        return checkpoint

    async def abort_episode(self, lease: EpisodeLease, error: BaseException) -> DiagnosticRef:
        self._require_open(lease)
        del self._open[lease.lease_id]
        return DiagnosticRef(f"diagnostic:{type(error).__name__}", lease.episode_id)

    async def confirm_shutdown(self, lease: EpisodeLease, proof: CleanShutdownProof) -> None:
        self._require_open(lease)
        if proof.episode_id != lease.episode_id:
            raise ValueError("shutdown proof belongs to another episode")
        self._stopped.add(lease.episode_id)

    async def restore(self, checkpoint: CheckpointRef) -> EpisodeLease:
        return self._lease(checkpoint.checkpoint_id)

    async def branch(self, checkpoint: CheckpointRef, count: int) -> list[EpisodeLease]:
        if count < 1:
            raise ValueError("branch count must be greater than zero")
        return [self._lease(checkpoint.checkpoint_id) for _ in range(count)]
