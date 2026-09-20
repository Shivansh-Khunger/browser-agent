from __future__ import annotations

import pytest

from browser_agent.browser import (
    BrowserAction,
    Observation,
    ObservationLimits,
    ScreenshotMetadata,
    SemanticControl,
    TargetHandle,
    TimeoutConfig,
    Viewport,
)


def test_observation_owns_handles_and_screenshot() -> None:
    viewport = Viewport(1280, 900)
    handle = TargetHandle("o1", "c7")
    observation = Observation(
        observation_id="o1",
        active_target_id="target-1",
        url="https://example.test",
        title="Example",
        document_generation=1,
        frame_generations={"main": 1},
        viewport=viewport,
        controls=(SemanticControl(handle, "button", "Buy"),),
        screenshot=ScreenshotMetadata("shot-1", "o1", "target-1", viewport),
    )

    assert str(observation.controls[0].handle) == "[o1:c7]"

    with pytest.raises(ValueError, match="another observation"):
        Observation(
            observation_id="o2",
            active_target_id="target-1",
            url="https://example.test",
            title="Example",
            document_generation=1,
            frame_generations={"main": 1},
            viewport=viewport,
            controls=observation.controls,
        )


def test_contract_values_are_validated_and_immutable() -> None:
    arguments = {"url": "https://example.test"}
    action = BrowserAction("navigate", arguments)
    arguments["url"] = "https://changed.test"

    assert action.arguments["url"] == "https://example.test"

    with pytest.raises(ValueError, match="potentially sensitive"):
        SemanticControl(
            TargetHandle("o1", "password"),
            "textbox",
            "Password",
            value="secret",
            potentially_sensitive=True,
        )

    with pytest.raises(ValueError, match="launch timeout"):
        TimeoutConfig(launch=0)


def test_observation_limits_enforce_positive_hard_ceilings() -> None:
    assert ObservationLimits(controls=1).controls == 1

    with pytest.raises(ValueError, match="positive integer"):
        ObservationLimits(context=0)
    with pytest.raises(ValueError, match="hard ceiling"):
        ObservationLimits(value=201)
