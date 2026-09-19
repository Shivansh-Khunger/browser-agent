# nodriver and Chromium browser-state primitives

## Decision

Use **one Chrome process and one unique `user_data_dir` per episode**, seeded from an immutable checkpoint. Make a full user-data-directory snapshot—only after graceful browser shutdown—the authoritative restorable and branchable state. Keep CDP cookies/storage exports, DOM snapshots, screenshots, and network logs as content-addressed observation artifacts and per-action deltas, not as substitutes for the profile checkpoint.

This design avoids shared mutable profiles while retaining Chromium-managed state that CDP cannot completely export and re-import.

## Why this boundary

nodriver exposes a custom `user_data_dir`; its quickstart says a supplied directory is retained, while its default generated profile is cleaned up. Its `Config` passes that directory to Chrome as `--user-data-dir`. nodriver also exposes raw CDP commands through `Tab.send`, so capture does not need a second automation stack. [nodriver quickstart](https://ultrafunkamsterdam.github.io/nodriver/nodriver/quickstart.html#custom-starting-options), [nodriver `Config` source](https://github.com/ultrafunkamsterdam/nodriver/blob/main/nodriver/core/config.py), [nodriver `Tab` documentation](https://ultrafunkamsterdam.github.io/nodriver/nodriver/classes/tab.html#nodriver.Tab.send)

Chromium says its user data directory holds profile data such as history, bookmarks, cookies, and other per-installation local state. Chromium storage partitions also cover persistent state visible to renderers, including cookies and local storage. A logical CDP export of only cookies and Web Storage is therefore incomplete. [Chromium user-data-directory documentation](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/user_data_dir.md), [Chromium `StoragePartition` interface](https://chromium.googlesource.com/chromium/src/+/master/content/public/browser/storage_partition.h)

CDP has targeted read/write APIs, not one comprehensive browser-state export/import API. In particular, the IndexedDB domain can enumerate databases and read records but exposes no general record-import operation; DOM snapshots and network logs are observations rather than executable browser state. Therefore, using a stopped profile directory as the authoritative checkpoint is an inference from the documented API surface. [CDP IndexedDB domain](https://chromedevtools.github.io/devtools-protocol/tot/IndexedDB/), [CDP DOMSnapshot domain](https://chromedevtools.github.io/devtools-protocol/tot/DOMSnapshot/), [CDP Network domain](https://chromedevtools.github.io/devtools-protocol/tot/Network/)

## Primitive inventory

| Need | Primitive | Use | Important limit |
|---|---|---|---|
| Persistent isolated episode | `nodriver.start(user_data_dir=...)` / `Config.user_data_dir` | Launch each episode against its own writable profile clone. | Never point two live episodes at one directory. |
| Ephemeral isolation within one process | `Target.createBrowserContext`, exposed by `Browser.create_context` | Useful for disposable incognito-like contexts or proxy separation. | CDP describes these contexts as empty/incognito-like and disposable; it provides no context snapshot/restore command. Do not use them as checkpoint authority. |
| Cookies | nodriver `browser.cookies.get_all()` / `set_all()` using `Storage.getCookies` / `Storage.setCookies` | Portable cookie observation and best-effort logical restore. Preserve all returned attributes, including partition information. | nodriver's `CookieJar.save()` is cookie-only and Python-pickle based; it is not a complete or durable browser checkpoint. |
| Local Storage | nodriver `Tab.get_local_storage()` / `set_local_storage()`; CDP `DOMStorage.getDOMStorageItems` and change events | Current-origin convenience plus storage-key-aware capture and deltas. | Storage is keyed; a current-page helper is not a cross-origin inventory. |
| Session Storage | CDP DOMStorage APIs and events | Observe values for known storage keys/namespaces. | Chromium identifies session storage by storage key **and namespace ID**, and ties persistence to tab/session restore. A flat key-value export loses this topology. |
| IndexedDB | `IndexedDB.requestDatabaseNames`, `requestDatabase`, `requestData`; `Storage.trackIndexedDBForStorageKey` | Inventory/schema/record artifacts and “content changed” invalidation signals. | No general CDP import path. Full fidelity comes from profile checkpoint. |
| Cache Storage | `CacheStorage` domain plus `Storage.trackCacheStorageForStorageKey` | Inventory cache names/entries and collect change signals. | Not a comprehensive import/restore mechanism. |
| DOM | `DOMSnapshot.captureSnapshot`; optionally `Page.captureSnapshot` (MHTML), `Page.captureScreenshot`, accessibility tree | Stable observation artifacts for evaluation, debugging, and comparisons. | DOM snapshots do not restore JS heap, workers, navigation stack, or browser storage. |
| Network | `Network.enable` plus request/response/loading/WebSocket events; `Network.getResponseBody` / `getRequestPostData` | Append-only request metadata; optional bounded bodies stored by hash. | Capture starts only after enablement. Bodies require explicit retrieval and buffer/size policy; secrets may appear in headers and bodies. |
| Tabs and targets | `Target.getTargets`, target lifecycle events, `Page.getFrameTree`, `Page.getNavigationHistory` | Record tab/frame topology and active URLs in checkpoint metadata. | Metadata can assist validation; it does not recreate live execution contexts by itself. |
| Browser identity | `Browser.getVersion` plus executable path/config | Record Chrome/CDP version for checkpoint compatibility. | Chromium supports forward migration better than downgrade; newer-written profiles may degrade under older Chrome. |

Sources: [nodriver browser and cookie source](https://github.com/ultrafunkamsterdam/nodriver/blob/main/nodriver/core/browser.py), [nodriver Tab API](https://ultrafunkamsterdam.github.io/nodriver/nodriver/classes/tab.html), [CDP Storage](https://chromedevtools.github.io/devtools-protocol/tot/Storage/), [CDP DOMStorage](https://chromedevtools.github.io/devtools-protocol/tot/DOMStorage/), [Chromium DOM Storage design](https://chromium.googlesource.com/chromium/src/+/main/content/browser/dom_storage/README.md), [CDP Target](https://chromedevtools.github.io/devtools-protocol/tot/Target/), [CDP Page](https://chromedevtools.github.io/devtools-protocol/tot/Page/), [CDP Browser](https://chromedevtools.github.io/devtools-protocol/1-3/Browser/), [Chromium user-data compatibility policy](https://chromium.googlesource.com/chromium/src/+/7b01f3a7773e7cc33a2ea377f8d5a524c4f134c8/docs/user_data_storage.md)

## Checkpoint lifecycle

1. **Create working state.** Allocate a new episode directory. For a fresh run, start empty. For resume or branch, copy/reflink an immutable checkpoint into that directory. Never make the checkpoint itself writable.
2. **Launch isolated browser.** Pass only that working directory as nodriver's `user_data_dir`. Record browser executable, `Browser.getVersion`, nodriver version, launch arguments, OS, episode ID, and parent-checkpoint ID.
3. **Capture action records.** Around each browser action, record action input/result, target/frame IDs, URL, timestamps, errors, screenshot/DOM artifact hashes, network events, and storage deltas described below.
4. **Seal checkpoint.** Stop accepting actions; flush capture queues; take final logical observations; request graceful shutdown with `Browser.close`; wait for Chrome process exit. Chromium documents that profile/service destruction and thread shutdown occur in stages, while nodriver's current `stop()` implementation ultimately terminates/kills the process, so checkpoint code should explicitly prefer CDP graceful close and verify exit before copying. [CDP `Browser.close`](https://chromedevtools.github.io/devtools-protocol/1-3/Browser/#method-close), [Chromium shutdown sequence](https://chromium.googlesource.com/chromium/src/+/master/docs/shutdown.md), [nodriver `Browser.stop` source](https://github.com/ultrafunkamsterdam/nodriver/blob/main/nodriver/core/browser.py)
5. **Snapshot and publish.** Copy/reflink the stopped working directory into temporary checkpoint storage, create a hash manifest, then atomically publish it as immutable. Exclude only proved-recomputable cache paths; default to full capture until restore tests establish safe exclusions.
6. **Restore validation.** Launch a new working clone, verify browser/profile version compatibility, tab/start URL expectations, cookie/storage sentinel values, and checkpoint manifest. Treat logical artifacts as validation evidence, not source of truth.

## Useful per-action deltas

Maintain an append-only event stream plus periodic canonical snapshots:

- Subscribe to `DOMStorage.domStorageItemAdded`, `domStorageItemUpdated`, `domStorageItemRemoved`, and `domStorageItemsCleared`. Include full storage key, local/session flag, and target/frame context. Periodically reconcile with `getDOMStorageItems` because subscriptions may begin late or targets may detach. [CDP DOMStorage](https://chromedevtools.github.io/devtools-protocol/tot/DOMStorage/)
- Enable `Storage.trackIndexedDBForStorageKey` and `trackCacheStorageForStorageKey` for discovered storage keys. Treat update events as invalidations: capture a new inventory or targeted data sample rather than pretending the event contains a replayable mutation. [CDP Storage](https://chromedevtools.github.io/devtools-protocol/tot/Storage/)
- Snapshot `Storage.getCookies` before/after each action and diff by cookie identity (`name`, `domain`, `path`, and partition key where present). CDP exposes cookie get/set/clear commands but no general cookie-change event in its Storage/Network domains. [CDP Storage](https://chromedevtools.github.io/devtools-protocol/tot/Storage/), [CDP Network cookie types](https://chromedevtools.github.io/devtools-protocol/tot/Network/#type-Cookie)
- Record `Network.requestWillBeSent`, `responseReceived`, `loadingFinished`, and `loadingFailed`, joined by request ID. Fetch bodies selectively after completion; content-address and compress them. Use allowlists, byte caps, and MIME filters. [CDP Network](https://chromedevtools.github.io/devtools-protocol/tot/Network/)
- Capture `DOMSnapshot.captureSnapshot` after actions when enabled. Hash normalized snapshots to avoid duplicate storage. Store full snapshots at configurable intervals and structural diffs between them. `DOMSnapshot` includes DOM/layout/style data for current documents; it remains an evaluation artifact, not a replay log. [CDP DOMSnapshot](https://chromedevtools.github.io/devtools-protocol/tot/DOMSnapshot/)

These deltas explain what changed and make evaluation cheap. They are not sufficient to reconstruct arbitrary browser state; restore always starts from a sealed profile checkpoint.

## Shared-profile hazards and controls

Chromium explicitly says two running Chrome instances cannot share one user data directory. Its process-singleton implementation uses locks/sockets keyed by that directory. Shared use can redirect startup into another process, return “profile in use,” or create cross-run interference rather than isolation. [Chromium user-data-directory documentation](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/user_data_dir.md), [Chromium `ProcessSingleton` source](https://chromium.googlesource.com/chromium/src/+/HEAD/chrome/browser/process_singleton.h)

Copying a live profile is unsafe by design inference: Chromium cookies use an asynchronous in-memory store backed by SQLite, local storage flushes pending work during shutdown, and Chrome has an ordered profile/service shutdown sequence. A filesystem copy taken while Chrome runs can therefore combine files from different logical instants or miss pending writes. Require verified process exit before snapshotting. [Chromium cookie storage](https://chromium.googlesource.com/chromium/src/+/main/net/cookies/README.md), [Chromium local-storage shutdown](https://chromium.googlesource.com/chromium/src/+/dd8d5985e4b1dc04142c94984d0654f4a3339298/components/services/storage/dom_storage/local_storage_impl.cc), [Chromium shutdown sequence](https://chromium.googlesource.com/chromium/src/+/master/docs/shutdown.md)

Other required controls:

- never use a developer's normal Chrome profile;
- one writer lease per episode working directory;
- immutable checkpoints with parent IDs, hashes, and atomic publication;
- redact or encrypt cookies, authorization headers, form values, storage values, screenshots, and response bodies before remote persistence;
- record Chrome version and reject unsupported downgrade restores, because Chromium documents degraded behavior when older builds open newer-written user data;
- separate cacheable observation blobs from secret-bearing authoritative checkpoints and apply different retention/access policies.

## Implementation contract for browser-agent

State adapter should expose these concepts, without binding callers to raw filesystem layout:

- `new_episode(base_checkpoint=None) -> EpisodeLease`
- `capture_after_action(action_record, policy) -> StateDelta`
- `seal_checkpoint(reason) -> CheckpointRef`
- `restore(checkpoint_ref) -> EpisodeLease`
- `branch(checkpoint_ref, count) -> list[EpisodeLease]`

`CheckpointRef` should identify immutable profile snapshot, manifest, parent, browser/nodriver versions, final logical-state digest, and observation-artifact roots. `StateDelta` should reference content-addressed artifacts and normalized cookie/storage/network/DOM changes. Full profile material stays private to adapter; no caller receives a shared mutable path.

## Answer to issue question

nodriver supplies persistent `user_data_dir`, cookie helpers, current-page local-storage helpers, target/context creation, and direct access to every CDP domain. CDP supplies isolated ephemeral browser contexts; cookies and storage-key APIs; DOM, page, accessibility, screenshot, and network capture; and mutation/invalidation events useful for deltas. Neither supplies a complete live-state serialization format. Restorable, branchable checkpoints should therefore be immutable copies of a uniquely owned profile directory made after graceful Chrome shutdown, with CDP artifacts layered beside them for observability and evaluation.
