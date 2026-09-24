# Browser Agent

Browser Agent lets one agent observe and operate one logical browser workspace across a task or interactive sequence of tasks.

## Language

**Browser session**:
The browser lifetime owned by one CLI process. Interactive tasks share it; a one-shot invocation owns one session.
_Avoid_: Run, browser instance

**Active tab**:
The sole page target the agent currently observes and acts upon. A newly opened tab replaces it only when adoption is unambiguous.
_Avoid_: Main tab, current page

**Browser action**:
One serialized attempt to change or directly inspect browser state through the active tab.
_Avoid_: Command, operation

**Human intervention**:
A point where automation cannot continue without a person, such as an unresolved verification challenge. Work may resume afterward, but the automation outcome remains failed even when the task ultimately succeeds.
_Avoid_: Manual step, user pause

**Automation outcome**:
Whether a task completed without human intervention. It is distinct from the task outcome.
_Avoid_: Task result, run result

**Harness**:
The caller that starts tasks, supplies configuration and user context, and consumes outcomes from the agent.
_Avoid_: CLI, environment

**User context**:
User-derived or auto-detected regional information supplied by the harness for agent reasoning. It does not imply browser emulation unless the harness explicitly requests an override.
_Avoid_: India defaults, browser identity

**Observation**:
One bounded semantic and visual description of the active tab at a point in time. A newer observation supersedes every target handle from an older one.
_Avoid_: Snapshot, page state

**Target handle**:
An observation-qualified reference to one actionable control. It is valid only for the observation that issued it.
_Avoid_: Element index, locator

**Semantic control**:
An actionable or state-bearing page entity derived from accessibility semantics and enriched with DOM identity.
_Avoid_: Element, node

**Context node**:
Unindexed semantic content that helps interpret controls, such as headings, labels, landmarks, status messages, and validation errors.
_Avoid_: Text element, non-actionable control

**Episode**:
One owned Chrome process using one unique writable browser profile. An episode may span multiple tasks and ends when its browser closes or checkpoint rollover begins.
_Avoid_: Task, run, browser session

**Checkpoint**:
An immutable, restorable browser profile sealed only after its episode's Chrome process exits cleanly.
_Avoid_: Snapshot, state file, backup

**State delta**:
Evidence describing browser-state changes associated with one browser action. It supports inspection and evaluation but cannot restore an episode.
_Avoid_: Checkpoint, replay event

**Artifact**:
Content-addressed evidence captured from an episode, such as semantic state, DOM data, screenshots, storage observations, or network records.
_Avoid_: Log file, checkpoint
