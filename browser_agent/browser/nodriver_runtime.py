# pyright: reportMissingTypeStubs=false, reportPrivateUsage=false
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnnecessaryComparison=false
"""Pinned nodriver launch and owned-process adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import nodriver
from nodriver import cdp
from nodriver.core.browser import Browser
from nodriver.core.config import Config, find_chrome_executable

from .models import (
    ActionResult,
    BrowserAction,
    BrowserConfig,
    BrowserEvaluationError,
    BrowserLaunchError,
    BrowserMetadata,
    BrowserShutdownError,
    ClosedTargetError,
    Observation,
    StaleTargetError,
)
from .nodriver_actions import click, navigate
from .nodriver_dom import ControlTarget, capture_observation
from .nodriver_frames import FrameRegistry, find_frame
from .runtime import OwnedBrowser, RuntimeFailure, RuntimeFailureKind


class NodriverLauncher:
    """Discover installed Chrome and launch only adapter-owned processes."""

    async def inspect(self, config: BrowserConfig) -> BrowserMetadata:
        executable = _resolve_executable(config.executable_path)
        version = await _browser_version(executable, config.timeouts.launch)
        digest = _config_digest(config)
        return BrowserMetadata(
            executable=executable,
            browser_version=version,
            nodriver_version=nodriver.__version__,
            platform=platform.platform(),
            config_digest=digest,
        )

    async def launch(
        self, config: BrowserConfig, profile: Path, metadata: BrowserMetadata
    ) -> OwnedBrowser:
        browser: Browser | None = None
        try:
            nodriver_config = Config(
                user_data_dir=profile,
                headless=config.headless,
                browser_executable_path=metadata.executable,
                browser_args=[f"--lang={config.locale}"] if config.locale else [],
                lang=config.locale,
            )
            browser = Browser(nodriver_config)
            async with asyncio.timeout(config.timeouts.launch):
                await browser.start()
                await _apply_context_overrides(browser, config)
            return NodriverOwnedBrowser(browser, config)
        except BaseException as error:
            if browser is not None:
                try:
                    await _discard_partial_browser(browser, min(1.0, config.timeouts.shutdown))
                except BrowserShutdownError as cleanup_error:
                    raise BrowserShutdownError(
                        "Chrome launch failed and partial process cleanup was not verified"
                    ) from cleanup_error
            if isinstance(error, asyncio.CancelledError):
                raise
            raise BrowserLaunchError(f"Chrome launch failed: {type(error).__name__}") from error


class NodriverOwnedBrowser:
    """Isolate exact-version nodriver internals needed for deterministic teardown."""

    def __init__(self, browser: Browser, config: BrowserConfig) -> None:
        process = browser._process
        if process is None or process.pid is None:
            raise BrowserLaunchError("nodriver did not return an owned Chrome process")
        self._browser = browser
        self._config = config
        self._process = process
        self._launch_target_id = _target_id(browser)
        self._observation_count = 0
        self._current_observation_id: str | None = None
        self._control_index: dict[str, ControlTarget] = {}
        self._frames = FrameRegistry(browser.main_tab)

    @property
    def process_id(self) -> int:
        return self._process.pid

    @property
    def active_target_id(self) -> str | None:
        return self._launch_target_id

    async def observe(self) -> Observation:
        self._require_active_tab()
        self._observation_count += 1
        observation_id = f"obs-{self._observation_count}"
        frame_root = await self._frames.reconcile()
        capture = await capture_observation(
            self._browser.main_tab,
            observation_id=observation_id,
            active_target_id=self._launch_target_id,
            frame_root=frame_root,
            limits=self._config.observation_limits,
        )
        self._current_observation_id = observation_id
        self._control_index = capture.control_index
        return capture.observation

    async def execute(self, action: BrowserAction) -> ActionResult:
        self._require_active_tab()
        if action.name == "navigate":
            result = await self._execute_navigate(action)
        elif action.name == "click":
            result = await self._execute_click(action)
        else:
            raise BrowserEvaluationError(f"unsupported browser action: {action.name!r}")
        self._require_active_tab()
        observation = await self.observe()
        return replace(result, observation=observation)

    async def _execute_navigate(self, action: BrowserAction) -> ActionResult:
        url = action.arguments.get("url")
        if not isinstance(url, str):
            raise ValueError("navigate requires a string 'url' argument")
        result = await navigate(self._browser.main_tab, self._config, url)
        return result

    async def _execute_click(self, action: BrowserAction) -> ActionResult:
        target = action.target
        if target is None:
            raise ValueError("click requires a target handle")
        if target.observation_id != self._current_observation_id:
            raise StaleTargetError(
                f"target belongs to observation {target.observation_id}; "
                f"current observation is {self._current_observation_id}"
            )
        control_target = self._control_index.get(target.control_id)
        if control_target is None:
            raise StaleTargetError(
                f"unknown control {target.control_id} for observation {target.observation_id}"
            )
        current_frame = find_frame(await self._frames.reconcile(), control_target.frame_id)
        if (
            current_frame is None
            or current_frame.session is not control_target.session
            or current_frame.document_generation != control_target.document_generation
        ):
            raise StaleTargetError(
                f"control {target.control_id} belongs to a stale frame document; "
                "take a fresh observation"
            )
        return await click(
            control_target.session,
            control_target.backend_node_id,
            self._config.timeouts,
        )

    def _require_active_tab(self) -> None:
        if _target_id(self._browser) != self._launch_target_id:
            raise ClosedTargetError("the owned tab is no longer the active target")

    async def wait_for_failure(self) -> RuntimeFailure:
        process_wait = asyncio.create_task(self._process.wait())
        listener = self._browser._listener_task
        waiters: set[asyncio.Future[object] | asyncio.Task[object]] = {process_wait}
        if listener is not None:
            waiters.add(listener)
        done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if process_wait in done:
            return RuntimeFailure(RuntimeFailureKind.PROCESS_EXIT, process_wait.result())
        process_wait.cancel()
        with suppress(asyncio.CancelledError):
            await process_wait
        return RuntimeFailure(RuntimeFailureKind.DISCONNECTED)

    async def request_close(self) -> None:
        with suppress(ConnectionError, BrokenPipeError):
            await self._browser.send(cdp.browser.close())

    async def close_connection(self) -> None:
        close_errors: list[Exception] = []
        try:
            await self._frames.close()
        except Exception as error:
            close_errors.append(error)
        try:
            await self._browser.aclose()
        except Exception as error:
            close_errors.append(error)
        if close_errors:
            raise ExceptionGroup("failed to close browser connections", close_errors)

    async def wait_for_exit(self) -> int:
        return await self._process.wait()

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    async def force_stop(self, timeout: float) -> None:
        with suppress(Exception):
            async with asyncio.timeout(timeout):
                await self.close_connection()
        await _terminate_process(self._process, timeout)


def _target_id(browser: Browser) -> str:
    target = cast(Any, browser.main_tab.target)
    return str(getattr(target, "target_id", target))


def _resolve_executable(configured: Path | None) -> Path:
    if configured is not None:
        discovered = str(configured)
    else:
        try:
            discovered = cast(str, find_chrome_executable())
        except FileNotFoundError as error:
            raise BrowserLaunchError(
                "No installed Chrome or Chromium executable found; configure executable_path"
            ) from error
    executable = Path(discovered).expanduser().resolve()
    if not executable.is_file():
        raise BrowserLaunchError(f"Browser executable does not exist: {executable}")
    return executable


async def _browser_version(executable: Path, timeout: float) -> str:
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            str(executable),
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate()
    except BaseException as error:
        if process is not None:
            try:
                await asyncio.shield(_terminate_process(process, min(1.0, timeout)))
            except BrowserShutdownError as cleanup_error:
                raise BrowserShutdownError(
                    "browser version process exit could not be verified"
                ) from cleanup_error
        if isinstance(error, asyncio.CancelledError):
            raise
        raise BrowserLaunchError("Could not inspect browser version") from error
    if process.returncode != 0:
        detail = (stderr or stdout).decode(errors="replace").strip()
        raise BrowserLaunchError(f"Browser version command failed: {detail}")
    version = stdout.decode(errors="replace").strip()
    if not version:
        raise BrowserLaunchError("Browser version command returned no version")
    return version


def _config_digest(config: BrowserConfig) -> str:
    redacted = json.dumps(
        {
            "allowed_domains": sorted(config.allowed_domains),
            "geolocation": config.geolocation,
            "headless": config.headless,
            "locale": config.locale,
            "observation_limits": {
                name: getattr(config.observation_limits, name)
                for name in (
                    "controls",
                    "context",
                    "name",
                    "description",
                    "value",
                    "context_text",
                )
            },
            "permissions": sorted(config.permissions),
            "timezone": config.timezone,
            "timeouts": {
                name: getattr(config.timeouts, name)
                for name in ("launch", "navigation", "action", "settle", "shutdown")
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(redacted).hexdigest()}"


async def _apply_context_overrides(browser: Browser, config: BrowserConfig) -> None:
    tab = browser.main_tab
    if config.timezone is not None:
        await tab.send(cdp.emulation.set_timezone_override(config.timezone))
    if config.geolocation is not None:
        latitude, longitude = config.geolocation
        await tab.send(
            cdp.emulation.set_geolocation_override(
                latitude=latitude,
                longitude=longitude,
                accuracy=100,
            )
        )
    if config.permissions:
        permissions = [cdp.browser.PermissionType(value) for value in config.permissions]
        await browser.send(cdp.browser.grant_permissions(permissions))


async def _discard_partial_browser(browser: Browser, timeout: float) -> None:
    with suppress(Exception):
        async with asyncio.timeout(timeout):
            await browser.aclose()
    process = browser._process
    if process is None or process.returncode is not None:
        return
    await _terminate_process(process, timeout)


async def _terminate_process(process: asyncio.subprocess.Process, timeout: float) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        async with asyncio.timeout(timeout):
            await process.wait()
            return
    except Exception:
        pass
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
        try:
            async with asyncio.timeout(timeout):
                await process.wait()
                return
        except Exception as error:
            raise BrowserShutdownError(
                "partial Chrome process exit could not be verified"
            ) from error
