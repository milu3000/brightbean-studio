# Dated MCP 2.0 contract fixture

`mcp-2026-07-28.json` contains unmodified selected definitions and their transitive
local references from the official MCP schema, retrieved 2026-10-02:
https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/schema/2026-07-28/schema.json

Full upstream schema SHA-256: `ef70b61f99b6d2e5e3b46863822eab08dff6a45bedc7a08914e0e5b133f40203`.

Source definitions: `DiscoverResult`, `ListToolsResult`, `CallToolResult`,
`RequestMetaObject`, `JSONRPCErrorResponse`, and `EmptyResult`. The fixture is
vendored so runtime metadata validation and conformance tests are deterministic
and never fetch schemas from the network. All $ref targets are local. Upstream license is included in `LICENSE.upstream`.

Tests also exercise the normative requirements which the schema cannot express:
per-request metadata; case-sensitive mirrored HTTP headers; encoded `Mcp-Name`;
HTTP 400/404 behavior; legacy fallback; typed events lifecycle; no batch dispatch;
origin validation; complete-result metadata; and private/no-store caching.

Events are specified in the OpenAI integration guide and draft extension, not in
the core schema. Their lifecycle/payload tests are authored locally rather than
misrepresented as an official Events conformance certification.

References:
- https://developers.openai.com/plugins/build/mcp-events
- https://modelcontextprotocol.io/specification/2026-07-28/basic/index
- https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
- https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- https://modelcontextprotocol.io/specification/2026-07-28/server/discover
- https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching
