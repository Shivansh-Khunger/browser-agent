from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from browser_agent.agent import Agent, AutomationOutcome, TaskResult, render_action_result
from browser_agent.browser import (
    ActionResult,
    BrowserAction,
    BrowserConfig,
    BrowserMetadata,
    Observation,
    OutcomeStatus,
    ScreenshotMetadata,
    SemanticControl,
    SessionLifecycle,
    TargetHandle,
    Viewport,
)
from browser_agent.cli import interactive, one_shot


def observation(sequence: int = 1, *, title: str = "Fixture") -> Observation:
    observation_id = f"o{sequence}"
    viewport = Viewport(1000, 700)
    return Observation(
        observation_id,
        "tab-1",
        f"https://example.test/{sequence}",
        title,
        sequence,
        {"main": sequence},
        viewport,
        controls=(
            SemanticControl(TargetHandle(observation_id, "c1"), "button", "Continue"),
            SemanticControl(TargetHandle(observation_id, "c2"), "textbox", "Email"),
        ),
        screenshot=ScreenshotMetadata(f"shot-{sequence}", observation_id, "tab-1", viewport),
    )


class FakeSession:
    def __init__(self) -> None:
        self.config = BrowserConfig(headless=True)
        self.lifecycle = SessionLifecycle.NEW
        self.active_target_id = "tab-1"
        self.metadata = BrowserMetadata(Path("/chrome"), "1", "0.50.3", "test", "digest")
        self.fatal_error = None
        self.restore_authority = None
        self.compatibility_warnings: tuple[str, ...] = ()
        self.actions: list[BrowserAction] = []
        self.current = observation()
        self.started = 0
        self.closed = 0
        self.invalidated = 0

    async def start(self) -> None:
        self.started += 1
        self.lifecycle = SessionLifecycle.RUNNING

    async def observe(self) -> Observation:
        return self.current

    async def execute(self, action: BrowserAction) -> ActionResult:
        self.actions.append(action)
        if action.name == "screenshot":
            return ActionResult(
                OutcomeStatus.SUCCEEDED,
                "screenshot captured",
                details={"data": "aGVsbG8="},
                observation=self.current,
            )
        self.current = observation(self.current.document_generation + 1)
        return ActionResult(
            OutcomeStatus.SUCCEEDED,
            f"{action.name} completed",
            details={"value": "ok"},
            observation=self.current,
        )

    async def checkpoint(self, reason: str) -> Any:
        return SimpleNamespace(reason=reason)

    async def invalidate(self, error: BaseException) -> None:
        assert isinstance(error, asyncio.CancelledError)
        self.invalidated += 1
        self.lifecycle = SessionLifecycle.CLOSED

    async def close(self) -> None:
        self.closed += 1
        self.lifecycle = SessionLifecycle.CLOSED


def tool_call(name: str, arguments: str, call_id: str = "call-1") -> Any:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def response(*calls: Any, content: str | None = None) -> Any:
    message = SimpleNamespace(content=content, tool_calls=list(calls))
    message.model_dump = lambda **_kwargs: {
        "role": "assistant",
        "content": content,
        "tool_calls": list(calls),
    }
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **_kwargs: Any) -> Any:
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_agent_executes_native_action_and_renders_structured_outcome() -> None:
    session = FakeSession()
    client = FakeClient(
        [
            response(tool_call("click", '{"index": 0}')),
            response(tool_call("done", '{"answer": "finished"}')),
        ]
    )
    agent = Agent(session, client=client, write=lambda _line: None)
    agent._vision_on = False
    await agent.start()

    result = await agent.run("continue", lambda _question: asyncio.sleep(0, result=""))

    assert result == TaskResult("finished", AutomationOutcome.SUCCEEDED)
    assert session.actions == [
        BrowserAction("click", target=TargetHandle("o1", "c1"), read_only=False)
    ]
    tool_message = next(message for message in agent.messages if message.get("role") == "tool")
    assert '"status": "succeeded"' in tool_message["content"]
    assert "Page outline:" in tool_message["content"]


@pytest.mark.asyncio
async def test_human_intervention_fails_only_current_automation_outcome() -> None:
    session = FakeSession()
    client = FakeClient(
        [
            response(tool_call("ask_user", '{"question": "Choose?"}')),
            response(tool_call("done", '{"answer": "first"}')),
            response(tool_call("done", '{"answer": "second"}')),
        ]
    )
    agent = Agent(session, client=client, write=lambda _line: None)
    agent._vision_on = False
    await agent.start()

    first = await agent.run("one", lambda _question: asyncio.sleep(0, result="choice"))
    second = await agent.run("two", lambda _question: asyncio.sleep(0, result=""))

    assert first.automation_outcome is AutomationOutcome.FAILED
    assert first.human_interventions == ("user_input",)
    assert second == TaskResult("second", AutomationOutcome.SUCCEEDED)


@pytest.mark.asyncio
async def test_cancellation_invalidates_session_and_waits_for_cleanup() -> None:
    class BlockingClient(FakeClient):
        async def create(self, **_kwargs: Any) -> Any:
            await asyncio.Event().wait()

    session = FakeSession()
    agent = Agent(session, client=BlockingClient([]), write=lambda _line: None)
    agent._vision_on = False
    await agent.start()
    task = asyncio.create_task(agent.run("wait", lambda _question: asyncio.sleep(0, result="")))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.invalidated == 1
    with pytest.raises(RuntimeError, match="not running"):
        await agent.run("again", lambda _question: asyncio.sleep(0, result=""))


@pytest.mark.asyncio
async def test_cli_modes_own_expected_session_lifetime() -> None:
    class FakeAgent:
        def __init__(self) -> None:
            self.started = 0
            self.closed = 0
            self.tasks: list[str] = []

        async def start(self) -> None:
            self.started += 1

        async def run(self, task: str, _ask: Any) -> TaskResult:
            self.tasks.append(task)
            return TaskResult(task, AutomationOutcome.SUCCEEDED)

        async def close(self) -> None:
            self.closed += 1

    interactive_agent = FakeAgent()
    inputs = iter(["first", "second", "/exit"])
    status = await interactive(
        agent_factory=lambda: interactive_agent,  # type: ignore[arg-type]
        input_fn=lambda _prompt: next(inputs),
        stdout=StringIO(),
        stderr=StringIO(),
    )
    assert status == 0
    assert interactive_agent.tasks == ["first", "second"]
    assert (interactive_agent.started, interactive_agent.closed) == (1, 1)

    one_shot_agent = FakeAgent()
    status = await one_shot(
        "only",
        agent_factory=lambda: one_shot_agent,  # type: ignore[arg-type]
        stdout=StringIO(),
    )
    assert status == 0
    assert one_shot_agent.tasks == ["only"]
    assert (one_shot_agent.started, one_shot_agent.closed) == (1, 1)


def test_action_result_hides_image_payload_but_keeps_outcome() -> None:
    rendered = render_action_result(
        ActionResult(
            OutcomeStatus.PARTIAL,
            "capture warning",
            error_code="capture_failed",
            details={"data": "secret-image-data", "completed_count": 2},
        )
    )
    assert "secret-image-data" not in rendered
    assert '"status": "partial"' in rendered
    assert '"completed_count": 2' in rendered
