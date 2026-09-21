# Rollback

Rollback is repository revert, not runtime selection.

1. Stop Browser Agent and verify owned Chrome process exited.
2. Preserve `AGENT_STATE_ROOT`, especially encrypted checkpoints and key.
3. Revert release commit or deploy previous repository revision.
4. Run previous revision's lockfile install and checks.
5. Start fresh episode. Resume checkpoint only when its compatibility rules allow
   current browser and schema versions.

Never add runtime backend selector, copy live profile, edit profile databases, or
force newer-browser checkpoint into older browser. If rollback revision cannot
read current checkpoint schema, keep checkpoint immutable and start fresh profile.
