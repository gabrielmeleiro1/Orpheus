# Desktop App Performance Design

## Scope

Improve the desktop application's perceived and measured performance without changing the conversion pipeline. The work covers shutdown, idle rendering, React update isolation, startup request duplication, and large-library browsing. Conversion semantics and model execution remain unchanged.

## Design

### Fast, bounded shutdown

Closing the last window will hide the UI immediately and start one idempotent background cleanup. The Tauri event loop will prevent only the first exit request; a completion flag allows the final programmatic exit through. Backend and setup-runner cleanup remain bounded and happen off the UI/event-loop thread.

The desktop Uvicorn server will close the event broker as soon as it receives its exit signal, before Uvicorn waits for active HTTP connections. This breaks the shutdown cycle in which the persistent SSE response remains active until FastAPI lifespan teardown. Uvicorn will also receive an explicit graceful-connection timeout so shutdown cannot wait indefinitely on a client connection.

### Idle graphics

Both dashboard canvases will render a static frame when their panel is hidden, the document is hidden, playback/conversion is inactive, or reduced motion is requested. Active animation remains capped at 30 fps. The conversion canvas will reuse its diffusion buffers instead of allocating a new typed array every frame.

### React update isolation

High-frequency playback, health, log, and conversion-progress changes will not force expensive hidden panels to recompute their trees. Panel boundaries will use stable callbacks and visibility-aware memoization; hook return values will be memoized where that makes prop identity meaningful. Health updates that contain no semantic change will retain their previous object identity.

### Startup request consolidation

Runtime model/capability bootstrap data will use a shared in-flight cache so independent hooks do not issue the same requests during one mount. Active-conversion reconciliation will have one initial request and one request per actual reconnect. The first library load will start immediately; only typed search changes remain debounced. Endpoint discovery will use the endpoint already returned by backend status rather than issuing a second command each poll.

A versioned, local readiness hint will let known-configured installations show the dashboard while the authoritative setup check runs in the background. New or unconfigured installations keep the lightweight setup-check state until the result is known, avoiding speculative construction of the full setup screen.

### Large libraries

Library data will be fetched in pages instead of loading up to 25,000 records eagerly. The first page is immediately useful, and users can progressively load more while the total count remains visible. The rendered list will be windowed so DOM cost stays bounded as pages accumulate. Search and mode changes reset pagination and preserve cancellation/stale-response protection.

## Error handling and compatibility

Shutdown remains best-effort and idempotent: cleanup errors are logged, but the process is still allowed to exit after the native deadline. Cached setup readiness is only an optimistic hint; the server response remains authoritative and clears stale hints. Request caches evict rejected promises. Pagination preserves existing search and mode semantics and never mixes responses from different queries.

## Verification

Tests will cover shutdown decision state and early SSE closure, static/active canvas scheduling, memoized hidden panels, bootstrap request deduplication, initial library timing, and paginated library state. The final pass will run Python tests, Vitest, TypeScript checks/build, Rust tests, and diff review.
