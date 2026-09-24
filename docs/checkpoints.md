# Checkpoint contract

Each episode owns unique writable Chrome profile cloned from immutable baseline or
checkpoint. Profiles are never shared by live episodes and are never copied while
Chrome runs.

Normal close stops actions, flushes required capture, closes Chrome, verifies
process exit, copies stopped profile, hashes manifest, encrypts restricted bytes,
and atomically publishes terminal `CheckpointRef`. Explicit mid-episode checkpoint
performs same seal, clones sealed result to new profile, relaunches, and continues
as child episode.

Checkpoint manifest records lineage, reason, clean-shutdown proof, encrypted
profile artifact, file digests, browser/nodriver/platform versions, logical-state
digest, artifact roots, schema, and creation time. Restore rejects corrupt
manifests and browser major-version downgrade. Newer browser is validated on
writable clone; nodriver-version change emits compatibility warning.

Failed shutdown quarantines working profile as diagnostic evidence and retains
last sealed checkpoint as restore authority. Failed seal is fatal. Repository
rollback does not downgrade checkpoint data; follow [rollback guide](rollback.md).
