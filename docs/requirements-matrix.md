# Requirements matrix

| Source | Resolved contract | Implementation | Tests | Documentation |
| --- | --- | --- | --- | --- |
| #2 | Unique writable profile; stopped immutable checkpoint; logical artifacts are evidence, not restore state | `state/local.py`, `state/checkpoints.py`, `state/artifacts.py`, `nodriver_session.py` | `test_local_state.py`, `test_state_contract.py`, `test_action_capture.py` | `architecture.md`, `checkpoints.md` |
| #3 | Async lifecycle; serialization; tab ownership; structured outcomes; budgets; no mutation retry; human intervention | `browser/interface.py`, `browser/models.py`, `nodriver_session.py`, `agent.py`, `cli.py` | `test_browser_contract.py`, `test_nodriver_lifecycle.py`, `test_nodriver_tabs.py`, `test_async_agent.py` | `architecture.md`, `operator.md` |
| #5 | Installed-browser discovery; CDP navigation/action/evaluation/screenshots; explicit popup adoption; deterministic teardown | `nodriver_runtime.py`, `nodriver_actions.py`, `transport.py` | `test_nodriver_lifecycle.py`, `test_nodriver_page_actions.py`, `test_nodriver_tabs.py` | `operator.md`, `cutover.md` |
| #6 | Adapter boundary; action evidence; redaction; content addressing; capture backpressure; encrypted checkpoints | `state/adapter.py`, `state/capture.py`, `state/artifacts.py`, `state/models.py` | `test_state_contract.py`, `test_action_capture.py`, `test_local_state.py`, `test_nodriver_workflows.py` | `architecture.md`, `checkpoints.md` |
| #7 | Bounded semantic observation; observation-scoped handles; frame order; native actions; screenshot-coordinate validation | `browser/models.py`, `nodriver_semantics.py`, `nodriver_frames.py`, `nodriver_actions.py`, `agent.py` | `test_models.py`, `test_nodriver_frames.py`, `test_nodriver_actions.py`, `test_nodriver_page_actions.py` | `operator.md`, `architecture.md` |
| #8 | AX/DOM identity enrichment; per-session iframe routing; pixel/protected fallback only | `nodriver_semantics.py`, `nodriver_dom.py`, `nodriver_frames.py`, `geometry.py` | `test_nodriver_frames.py`, `test_nodriver_actions.py` | `architecture.md`, `incompatibilities.md` |

No requirement relies on compatibility layer or runtime backend selector.
