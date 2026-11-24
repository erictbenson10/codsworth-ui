# Public UI export

This is a clean source snapshot of the Codsworth interface, derived from the maintained project at revision `44c813f`. Private repository history, backend deployment instructions, service verification logs, model weights, training recordings, conversation state, and third-party reference photographs are excluded.

The terminal client’s dependency closure is preserved. Public-release changes replace machine-specific launch behavior with an explicit gateway URL and make browser audio demos accept a local file. Existing rendering, workstream, input, interruption, and animation behavior remains covered by the included tests.

The screenshots and silent videos use fictional demo content. Terminal captures come from the actual `Terminal.draw` renderer, with its screen cells captured by a small adapter; the browser does not reconstruct a substitute chat layout. Browser character captures use the actual animator and interaction handlers.

Codsworth and Fallout are associated with their original rights holders. This is an independent fan project, not an official product. No game recordings or reference photographs are redistributed in this UI repository. Font licensing is retained alongside the bundled font files. The source snapshot does not assign a new blanket license to inherited material.

## Integration boundary

The client expects the Codsworth HTTP/streaming gateway protocol. Its `--url` option or `CODS_URL` selects that gateway. Native tool execution, model inference, transcription, issue tracking, and Fleet controls require separately configured services. Their appearance in a command menu does not mean those services are enabled or reachable.

Default issue-repository labels in the public snapshot use example names. Configure the gateway for your own repositories before enabling integration writes. Local tasks and drafts persist under the client’s normal user state directory; media capture uses a disposable temporary directory instead.

Supporting issue and Fleet reader modules are retained for the client’s failure-path tests. Their example origins and repository allowlists are fixtures, not a deployable gateway configuration. A Gitea connector also requires an explicit `CODS_GITEA_TOKEN_FILE`; no private deployment credential path is assumed.
