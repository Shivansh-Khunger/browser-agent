# Architecture

Browser Agent has one async path: harness configuration enters `Agent`, model
tools become `BrowserAction` values, and `NodriverSession` serializes them against
one active tab. Public code depends on `BrowserSession`; CDP and nodriver types
stay behind browser transport modules.

Session lifecycle is `NEW -> RUNNING -> CLOSING -> CLOSED`. Startup happens once.
Disconnect, unexpected browser exit, or teardown failure makes session unusable;
there is no reconnect or alternate runtime.

Each browser action validates session and active tab, dispatches at most once,
settles within configured budget, repairs active-tab ownership, captures required
evidence, and returns structured status. Reads share serialization but skip
post-action settling. See [operator guide](operator.md) for operational behavior.

Semantic observations traverse main document, frames, and visible shadow roots in
deterministic document order. Handles combine observation ID with internal control
ID. Native pointer actions revalidate backend node identity, scroll, compute
current geometry, hit-test, then dispatch through owning CDP session. Coordinate
clicks require matching observation and screenshot IDs.

State boundary allocates unique writable profile per episode. Browser session
cannot publish writable profile path. Redacted logical evidence and encrypted
profile checkpoints use separate artifact security classes.
