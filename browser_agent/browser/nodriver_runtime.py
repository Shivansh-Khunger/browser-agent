# pyright: reportMissingTypeStubs=false, reportPrivateUsage=false
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnnecessaryComparison=false
"""Pinned nodriver launch and owned-process adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import platform
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import nodriver
from nodriver import cdp
from nodriver.core.browser import Browser
from nodriver.core.config import Config, find_chrome_executable
from nodriver.core.tab import Tab

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
    OutcomeStatus,
    ScreenshotMetadata,
    SemanticControl,
    StaleTargetError,
    TargetHandle,
)
from .nodriver_actions import (
    click,
    click_at,
    navigate,
    select_option,
    type_otp,
    type_text,
)
from .nodriver_dom import ControlTarget, capture_observation, read_viewport
from .nodriver_frames import FrameRegistry, find_frame, frame_generations
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
                browser_args=[
                    "--disable-popup-blocking",
                    *([f"--lang={config.locale}"] if config.locale else []),
                ],
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
        self._launch_tab = browser.main_tab
        self._launch_target_id = _tab_target_id(self._launch_tab)
        self._active_tab = self._launch_tab
        self._active_target_id = self._launch_target_id
        self._target_openers: dict[str, str | None] = {
            self._launch_target_id: _tab_opener_id(self._launch_tab)
        }
        self._adoption_order: list[str] = []
        self._observation_count = 0
        self._current_observation_id: str | None = None
        self._current_screenshot: ScreenshotMetadata | None = None
        self._current_frame_generations: dict[str, int] = {}
        self._control_index: dict[str, ControlTarget] = {}
        self._control_semantics: dict[str, SemanticControl] = {}
        self._redacted_backend_node_ids: set[object] = set()
        self._redaction_tokens: set[str] = set()
        self._frames = FrameRegistry(self._active_tab)

    @property
    def process_id(self) -> int:
        return self._process.pid

    @property
    def active_target_id(self) -> str | None:
        return self._active_target_id

    async def observe(self) -> Observation:
        await self._repair_active_tab()
        self._observation_count += 1
        observation_id = f"obs-{self._observation_count}"
        frame_root = await self._frames.reconcile()
        capture = await capture_observation(
            self._active_tab,
            observation_id=observation_id,
            active_target_id=self._active_target_id,
            frame_root=frame_root,
            limits=self._config.observation_limits,
        )
        sensitive_bounds = tuple(
            (control.frame_breadcrumb, control.bounds)
            for control in capture.observation.controls
            if (
                capture.control_index[control.handle.control_id].backend_node_id
                in self._redacted_backend_node_ids
            )
            and control.bounds is not None
        )

        def redact(control: SemanticControl) -> SemanticControl:
            backend_node_id = capture.control_index[control.handle.control_id].backend_node_id
            if backend_node_id in self._redacted_backend_node_ids:
                return replace(control, potentially_sensitive=True, value=None)
            if any(
                control.frame_breadcrumb == breadcrumb and _bounds_contain(bounds, control.bounds)
                for breadcrumb, bounds in sensitive_bounds
            ):
                # Chromium can surface live input text as a separate AX text
                # descendant. It has a different backend ID, so redact it by
                # its containing field's current bounds as well.
                return replace(
                    control,
                    potentially_sensitive=True,
                    name="",
                    description="",
                    value=None,
                )
            return control

        controls = tuple(redact(control) for control in capture.observation.controls)
        observation = replace(
            capture.observation,
            controls=tuple(
                replace(
                    control,
                    name=self._redact_text(control.name),
                    description=self._redact_text(control.description),
                    value=self._redact_text(control.value) if control.value is not None else None,
                )
                for control in controls
            ),
            context=tuple(
                replace(node, text=self._redact_text(node.text))
                for node in capture.observation.context
            ),
        )
        self._current_observation_id = observation_id
        self._current_screenshot = observation.screenshot
        self._current_frame_generations = dict(observation.frame_generations)
        self._control_index = capture.control_index
        self._control_semantics = {
            control.handle.control_id: control for control in observation.controls
        }
        return observation

    async def execute(self, action: BrowserAction) -> ActionResult:
        before = await self._repair_active_tab()
        source_target_id = self._active_target_id
        if action.name == "navigate":
            result = await self._execute_navigate(action)
        elif action.name == "click":
            result = await self._execute_click(action)
        elif action.name == "click_at":
            result = await self._execute_click_at(action)
        elif action.name == "type":
            result = await self._execute_type(action)
        elif action.name == "fill_form":
            result = await self._execute_fill_form(action)
        elif action.name == "type_otp":
            result = await self._execute_type_otp(action)
        elif action.name == "select_option":
            result = await self._execute_select_option(action)
        elif action.name == "scroll":
            result = await self._execute_scroll(action)
        elif action.name == "back":
            result = await self._execute_back()
        elif action.name == "read_page":
            result = await self._execute_read_page()
        elif action.name == "get_html":
            result = await self._execute_get_html()
        elif action.name == "screenshot":
            result = await self._execute_screenshot()
        elif action.name == "viewport":
            result = await self._execute_viewport()
        elif action.name == "evaluate":
            result = await self._execute_evaluate(action)
        else:
            raise BrowserEvaluationError(f"unsupported browser action: {action.name!r}")
        ambiguity = await self._reconcile_action_targets(before, source_target_id, result)
        observation = await self.observe()
        return replace(ambiguity or result, observation=observation)

    async def _execute_navigate(self, action: BrowserAction) -> ActionResult:
        url = action.arguments.get("url")
        if not isinstance(url, str):
            raise ValueError("navigate requires a string 'url' argument")
        result = await navigate(self._active_tab, self._config, url)
        return result

    async def _execute_click(self, action: BrowserAction) -> ActionResult:
        control_target, _control = await self._resolve_control(action.target, "click")
        return await click(
            control_target.session,
            control_target.backend_node_id,
            self._config.timeouts,
        )

    async def _resolve_control(
        self, target: object, action_name: str
    ) -> tuple[ControlTarget, SemanticControl]:
        if not isinstance(target, TargetHandle):
            raise ValueError(f"{action_name} requires a target handle")
        if target.observation_id != self._current_observation_id:
            raise StaleTargetError(
                f"target belongs to observation {target.observation_id}; "
                f"current observation is {self._current_observation_id}"
            )
        control_target = self._control_index.get(target.control_id)
        control = self._control_semantics.get(target.control_id)
        if control_target is None or control is None:
            raise StaleTargetError(
                f"unknown control {target.control_id} for observation {target.observation_id}"
            )
        current_root = await self._frames.reconcile()
        if frame_generations(current_root) != self._current_frame_generations:
            raise StaleTargetError(
                f"control {target.control_id} belongs to stale frame documents; "
                "take a fresh observation"
            )
        current_frame = find_frame(current_root, control_target.frame_id)
        if (
            current_frame is None
            or current_frame.session is not control_target.session
            or current_frame.document_generation != control_target.document_generation
        ):
            raise StaleTargetError(
                f"control {target.control_id} belongs to a stale frame document; "
                "take a fresh observation"
            )
        return control_target, control

    async def _execute_type(self, action: BrowserAction) -> ActionResult:
        text = action.arguments.get("text")
        if not isinstance(text, str):
            return _failed_action("invalid_arguments", "type requires a string 'text' argument")
        submit = action.arguments.get("submit", False)
        if not isinstance(submit, bool):
            return _failed_action("invalid_arguments", "type 'submit' must be a boolean")
        return await self._type_target(action.target, text, submit)

    async def _type_target(self, target: object, text: str, submit: bool) -> ActionResult:
        try:
            control_target, control = await self._resolve_control(target, "type")
        except (StaleTargetError, ValueError) as error:
            return _failed_action(_target_error_code(error), "text target is stale or unavailable")
        if "disabled" in control.states:
            return _failed_action("disabled", "text control is disabled")
        if "readonly" in control.states:
            return _failed_action("readonly", "text control is readonly")
        if not control.visible:
            return _failed_action("target_obscured", "text control is not visible")
        result = await type_text(
            control_target.session,
            control_target.backend_node_id,
            text,
            submit=submit,
            timeouts=self._config.timeouts,
        )
        if result.status in {OutcomeStatus.SUCCEEDED, OutcomeStatus.UNCERTAIN}:
            # Text supplied to browser actions commonly contains credentials,
            # addresses, codes, or other secrets. Once a control receives text,
            # future observations never record its value for this episode.
            self._redacted_backend_node_ids.add(control_target.backend_node_id)
            if text:
                self._redaction_tokens.add(text)
        return result

    async def _execute_fill_form(self, action: BrowserAction) -> ActionResult:
        fields = action.arguments.get("fields")
        if not isinstance(fields, (list, tuple)):
            return _failed_action("invalid_arguments", "fill_form requires a 'fields' list")
        completed: list[str] = []
        for field in fields:
            if not isinstance(field, dict):
                return _form_stop(completed, "invalid_arguments", "form field is invalid")
            text = field.get("text")
            submit = field.get("submit", False)
            target = field.get("target")
            if not isinstance(text, str) or not isinstance(submit, bool):
                return _form_stop(
                    completed, "invalid_arguments", "form field arguments are invalid"
                )
            result = await self._type_target(target, text, submit)
            if result.status is not OutcomeStatus.SUCCEEDED:
                return _form_stop(completed, result.error_code or "field_failed", result.message)
            if isinstance(target, TargetHandle):
                completed.append(target.control_id)
        return ActionResult(
            OutcomeStatus.SUCCEEDED,
            "form input completed",
            details={"completed_fields": tuple(completed), "completed_count": len(completed)},
        )

    async def _execute_type_otp(self, action: BrowserAction) -> ActionResult:
        code = action.arguments.get("code")
        if not isinstance(code, str):
            return _failed_action("invalid_arguments", "type_otp requires a string 'code' argument")
        try:
            control_target, control = await self._resolve_control(action.target, "type_otp")
        except (StaleTargetError, ValueError) as error:
            return _failed_action(
                _target_error_code(error), "verification-code target is stale or unavailable"
            )
        if "disabled" in control.states:
            return _failed_action("disabled", "verification-code control is disabled")
        if "readonly" in control.states:
            return _failed_action("readonly", "verification-code control is readonly")
        result = await type_otp(
            control_target.session, control_target.backend_node_id, code, self._config.timeouts
        )
        if result.status in {OutcomeStatus.SUCCEEDED, OutcomeStatus.UNCERTAIN}:
            self._redacted_backend_node_ids.add(control_target.backend_node_id)
            if code:
                self._redaction_tokens.add(code)
        return result

    async def _execute_select_option(self, action: BrowserAction) -> ActionResult:
        label = action.arguments.get("label", action.arguments.get("value"))
        if not isinstance(label, str):
            return _failed_action(
                "invalid_arguments", "select_option requires a string 'label' argument"
            )
        try:
            control_target, control = await self._resolve_control(action.target, "select_option")
        except (StaleTargetError, ValueError) as error:
            return _failed_action(
                _target_error_code(error), "select target is stale or unavailable"
            )
        if "disabled" in control.states:
            return _failed_action("disabled", "select control is disabled")
        if not control.visible:
            return _failed_action("target_obscured", "select control is not visible")
        return await select_option(
            control_target.session, control_target.backend_node_id, label, self._config.timeouts
        )

    async def _execute_scroll(self, action: BrowserAction) -> ActionResult:
        direction = action.arguments.get("direction")
        if direction not in {"up", "down"}:
            return _failed_action("invalid_arguments", "scroll direction must be 'up' or 'down'")
        viewport = await read_viewport(self._active_tab)
        delta = viewport.height * (0.9 if direction == "down" else -0.9)
        await self._active_tab.send(
            cdp.input_.dispatch_mouse_event(
                "mouseWheel", x=viewport.width / 2, y=viewport.height / 2, delta_y=delta
            )
        )
        await asyncio.sleep(self._config.timeouts.settle)
        return ActionResult(OutcomeStatus.SUCCEEDED, f"scrolled {direction}")

    async def _execute_back(self) -> ActionResult:
        current_index, entries = await self._active_tab.send(cdp.page.get_navigation_history())
        if current_index <= 0 or not entries:
            return _failed_action("history_empty", "no previous page is available")
        await self._active_tab.send(
            cdp.page.navigate_to_history_entry(entries[current_index - 1].id_)
        )
        await asyncio.sleep(self._config.timeouts.settle)
        return ActionResult(OutcomeStatus.SUCCEEDED, "back navigation dispatched")

    async def _execute_read_page(self) -> ActionResult:
        return await self._evaluate_read(
            """(() => (document.body ? document.body.innerText : '').replace(/\\n{3,}/g, '\\n\\n')
                .trim().slice(0, 8000))()""",
            "page read completed",
            "text",
        )

    async def _execute_get_html(self) -> ActionResult:
        return await self._evaluate_read(
            """(() => {
                const root = document.documentElement.cloneNode(true);
                root.querySelectorAll('script,style,noscript,svg,link,meta,template').forEach(
                    (node) => node.remove());
                root.querySelectorAll('input[type=password],input[autocomplete*=password]').forEach(
                    (node) => { node.value = ''; node.setAttribute('value', ''); });
                return root.outerHTML.replace(/\\s{2,}/g, ' ').slice(0, 14000);
            })()""",
            "HTML read completed",
            "html",
        )

    async def _evaluate_read(self, expression: str, message: str, detail_name: str) -> ActionResult:
        result, exception = await self._active_tab.send(
            # This expression is adapter-owned and operates on a detached clone
            # when it needs DOM cleanup; V8 otherwise rejects it as a possible
            # side effect despite no mutation of the active document.
            cdp.runtime.evaluate(expression, return_by_value=True)
        )
        if exception is not None:
            return _failed_action("evaluation_failed", f"{message[:-10]} failed")
        value = result.value if isinstance(result.value, str) else ""
        return ActionResult(
            OutcomeStatus.SUCCEEDED, message, details={detail_name: self._redact_text(value)}
        )

    async def _execute_screenshot(self) -> ActionResult:
        data = await self._active_tab.send(
            cdp.page.capture_screenshot(
                format_="png", from_surface=True, capture_beyond_viewport=False
            )
        )
        return ActionResult(
            OutcomeStatus.SUCCEEDED,
            "screenshot captured",
            details={"mime_type": "image/png", "data": data},
        )

    async def _execute_viewport(self) -> ActionResult:
        viewport = await read_viewport(self._active_tab)
        return ActionResult(
            OutcomeStatus.SUCCEEDED,
            "viewport read completed",
            details={
                "width": viewport.width,
                "height": viewport.height,
                "device_scale": viewport.device_scale,
                "offset_x": viewport.offset_x,
                "offset_y": viewport.offset_y,
                "scale": viewport.scale,
                "scroll_x": viewport.scroll_x,
                "scroll_y": viewport.scroll_y,
            },
        )

    async def _execute_evaluate(self, action: BrowserAction) -> ActionResult:
        expression = action.arguments.get("expression")
        if not isinstance(expression, str):
            return _failed_action(
                "invalid_arguments", "evaluate requires a string 'expression' argument"
            )
        result, exception = await self._active_tab.send(
            cdp.runtime.evaluate(
                expression,
                return_by_value=True,
                throw_on_side_effect=True if action.read_only else None,
            )
        )
        if exception is not None:
            return _failed_action("evaluation_failed", "JavaScript evaluation failed")
        value = self._redact_text(result.value) if isinstance(result.value, str) else result.value
        return ActionResult(
            OutcomeStatus.SUCCEEDED,
            "JavaScript evaluation completed",
            details={"value": value},
        )

    def _redact_text(self, value: str) -> str:
        redacted = value
        for token in self._redaction_tokens:
            redacted = redacted.replace(token, "[REDACTED]")
        return redacted

    async def _execute_click_at(self, action: BrowserAction) -> ActionResult:
        observation_id = action.arguments.get("observation_id")
        screenshot_id = action.arguments.get("screenshot_id")
        if observation_id != self._current_observation_id:
            raise StaleTargetError(
                f"coordinate target belongs to observation {observation_id!r}; "
                f"current observation is {self._current_observation_id!r}"
            )
        screenshot = self._current_screenshot
        if screenshot is None or screenshot_id != screenshot.screenshot_id:
            raise StaleTargetError("screenshot is missing, mismatched, or stale")
        if screenshot.active_target_id != self._active_target_id:
            raise StaleTargetError("screenshot belongs to a stale browser target")
        if frame_generations(await self._frames.reconcile()) != self._current_frame_generations:
            raise StaleTargetError("screenshot belongs to stale frame documents")
        if await read_viewport(self._active_tab) != screenshot.viewport:
            raise StaleTargetError("screenshot viewport changed; take a fresh observation")
        x = _finite_coordinate(action.arguments.get("x"), "x")
        y = _finite_coordinate(action.arguments.get("y"), "y")
        width = float(screenshot.pixel_width or screenshot.viewport.width)
        height = float(screenshot.pixel_height or screenshot.viewport.height)
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError("click_at coordinates must be inside the screenshot viewport")
        css_x = x * screenshot.viewport.width / width
        css_y = y * screenshot.viewport.height / height
        return await click_at(self._active_tab, css_x, css_y, self._config.timeouts)

    async def _page_tabs(self) -> dict[str, Tab]:
        await self._browser.update_targets()
        tabs = {_tab_target_id(cast(Tab, tab)): cast(Tab, tab) for tab in self._browser.tabs}
        for target_id, tab in tabs.items():
            self._target_openers[target_id] = _tab_opener_id(tab)
        return tabs

    async def _repair_active_tab(self) -> dict[str, Tab]:
        tabs = await self._page_tabs()
        current = tabs.get(self._active_target_id)
        if current is not None:
            self._active_tab = current
            return tabs

        opener_id = self._target_openers.get(self._active_target_id)
        fallback_ids = (
            ([opener_id] if opener_id is not None else [])
            + list(reversed(self._adoption_order))
            + [self._launch_target_id]
        )
        for target_id in fallback_ids:
            tab = tabs.get(target_id)
            if tab is not None:
                await self._set_active_tab(tab, adopted=False)
                return tabs
        raise ClosedTargetError("active tab closed and no owned fallback tab survives")

    async def _reconcile_action_targets(
        self,
        before: dict[str, Tab],
        source_target_id: str,
        action_result: ActionResult,
    ) -> ActionResult | None:
        after = await self._page_tabs()
        candidates = sorted(set(after) - set(before))
        if len(candidates) == 1:
            await self._set_active_tab(after[candidates[0]], adopted=True)
            return None
        if len(candidates) > 1:
            # Chrome may foreground one new tab, but inventory order and browser
            # focus are not ownership signals. Keep the logical active tab.
            if source_target_id in after:
                await after[source_target_id].activate()
                self._active_tab = after[source_target_id]
                self._active_target_id = source_target_id
            else:
                await self._repair_active_tab()
            details = tuple(_target_details(after[target_id]) for target_id in candidates)
            return ActionResult(
                OutcomeStatus.UNCERTAIN,
                "browser action created multiple plausible child tabs; active tab was retained",
                error_code="ambiguous_target",
                retryable=False,
                details={
                    "candidates": details,
                    "candidate_count": len(details),
                    "action_status": action_result.status.value,
                    "action_message": action_result.message,
                },
            )
        await self._repair_active_tab()
        return None

    async def _set_active_tab(self, tab: Tab, *, adopted: bool) -> None:
        target_id = _tab_target_id(tab)
        await tab.activate()
        if target_id == self._active_target_id:
            self._active_tab = tab
            return
        await self._frames.close()
        self._active_tab = tab
        self._active_target_id = target_id
        self._frames = FrameRegistry(tab)
        self._current_observation_id = None
        self._current_screenshot = None
        self._current_frame_generations = {}
        self._control_index = {}
        self._control_semantics = {}
        if adopted:
            self._adoption_order = [
                adopted_id for adopted_id in self._adoption_order if adopted_id != target_id
            ]
            self._adoption_order.append(target_id)

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


def _tab_target_id(tab: Tab) -> str:
    target = cast(Any, tab.target)
    return str(getattr(target, "target_id", target))


def _tab_opener_id(tab: Tab) -> str | None:
    opener_id = getattr(cast(Any, tab.target), "opener_id", None)
    return str(opener_id) if opener_id is not None else None


def _target_details(tab: Tab) -> dict[str, object]:
    target = cast(Any, tab.target)
    return {
        "target_id": _tab_target_id(tab),
        "url": str(getattr(target, "url", "")),
        "title": str(getattr(target, "title", "")),
        "opener_id": _tab_opener_id(tab),
    }


def _finite_coordinate(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"click_at {name} coordinate must be a finite number")
    coordinate = float(value)
    if not math.isfinite(coordinate):
        raise ValueError(f"click_at {name} coordinate must be a finite number")
    return coordinate


def _failed_action(code: str, message: str) -> ActionResult:
    return ActionResult(OutcomeStatus.FAILED, message, error_code=code, retryable=False)


def _target_error_code(error: Exception) -> str:
    return "stale_target" if isinstance(error, StaleTargetError) else "invalid_target"


def _form_stop(completed: list[str], code: str, message: str) -> ActionResult:
    status = OutcomeStatus.PARTIAL if completed else OutcomeStatus.FAILED
    return ActionResult(
        status,
        f"form input stopped: {message}",
        error_code=code,
        retryable=False,
        details={"completed_fields": tuple(completed), "completed_count": len(completed)},
    )


def _bounds_contain(
    outer: tuple[float, float, float, float] | None,
    inner: tuple[float, float, float, float] | None,
) -> bool:
    if outer is None or inner is None:
        return False
    left, top, right, bottom = outer
    inner_left, inner_top, inner_right, inner_bottom = inner
    return (
        left <= inner_left and top <= inner_top and inner_right <= right and inner_bottom <= bottom
    )


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
