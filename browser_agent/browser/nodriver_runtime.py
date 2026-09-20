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
    Observation,
)
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
            return NodriverOwnedBrowser(browser)
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

    def __init__(self, browser: Browser) -> None:
        process = browser._process
        if process is None or process.pid is None:
            raise BrowserLaunchError("nodriver did not return an owned Chrome process")
        self._browser = browser
        self._process = process
        target = cast(Any, browser.main_tab.target)
        self._launch_target_id = str(getattr(target, "target_id", target))

    @property
    def process_id(self) -> int:
        return self._process.pid

    @property
    def active_target_id(self) -> str | None:
        return self._launch_target_id

    async def observe(self) -> Observation:
        raise BrowserEvaluationError("semantic observation is implemented by issue #12")

    async def execute(self, action: BrowserAction) -> ActionResult:
        del action
        raise BrowserEvaluationError("browser actions are implemented by issue #12")

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
        await self._browser.aclose()

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


def _resolve_executable(configured: Path | None) -> Path:
    discovered = (
        str(configured) if configured is not None else cast(str | None, find_chrome_executable())
    )
    if not discovered:
        raise BrowserLaunchError(
            "No installed Chrome or Chromium executable found; configure executable_path"
        )
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
