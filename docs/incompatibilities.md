# Known incompatibilities

- Python older than 3.11 is unsupported.
- Browser binary auto-download is unsupported. Install Chrome/Chromium or set
  `AGENT_BROWSER_EXECUTABLE`.
- Checkpoint written by newer Chrome major cannot restore into older Chrome.
- Checkpoints from pre-CDP Firefox implementation are not migrated.
- Shared writable profiles and live profile copies are unsupported.
- Closed/protected roots, pixel-only canvases, persistently obscured controls, and
  intentionally protected UI may require screenshot coordinates or human input.
- Automatic reconnect and automatic mutation retry are intentionally unsupported.
- Headless mode may trigger site-specific verification more often than headed mode.
