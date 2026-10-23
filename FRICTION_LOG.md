# Friction log

Issues hit while building with the Ring Partner API, Ring Developer Playground, Ring docs, Amazon Bedrock and the MCP Python SDK. Each entry is backed by a recorded fixture, a test, a results file or a quoted error or doc, and uses the format: task, steps, expected vs. actual, severity, workaround, suggestion.

## Ring Partner API

**1. WHEP returns 201 and connects for an offline device, then sends no media**
- **Task:** Capture the Playground Package live-view event over WHEP.
- **Steps:** `GET /v1/devices?include=status` returned `online: false` for the Playground Device. Then `POST .../media/streaming/whep/sessions`.
- **Expected:** `503 Device Unavailable` ("Device is currently offline"), as the WHEP docs say.
- **Actual:** `201` with a valid SDP answer. ICE and DTLS connected, but `getStats()` showed zero inbound RTP packets over 20 s. Recorded as `tests/fixtures/ring/devices_list_offline.json`.
- **Severity:** Medium. A client waits out its whole timeout, and "connected" looks healthy.
- **Workaround:** Check `online` from device discovery and refuse offline devices before opening WHEP.
- **Suggestion:** Return the documented 503, and document how the Playground device's online state relates to the selected event.

**2. The WHEP Location header has an undocumented query parameter, and DELETE's response is undocumented**
- **Task:** Close a WHEP session.
- **Steps:** `POST .../whep/sessions`, then read the `Location` header and `DELETE` it.
- **Expected:** `https://api.amazonvision.com/v1/devices/{device_id}/media/streaming/whep/sessions/{session_id}`, per the docs' "Session URL Structure".
- **Actual:** `.../whep/sessions/{uuid}?location=ava1.ring.whep.location.<~300-char token>`. A client that rebuilds the URL from the IDs drops the query. DELETE on the verbatim URL returned `200` with an empty body; the docs give no status for DELETE. (We didn't test DELETE without the query.)
- **Severity:** Low.
- **Workaround:** Store the `Location` header verbatim and DELETE exactly that URL (tested against the recorded fixture).
- **Suggestion:** Show the query in the docs, say the Location URL is opaque, and document the DELETE response.

**3. Event History ignores `event_types` in the Playground, and items carry undocumented fields**
- **Task:** Record Event History responses for the polling fallback.
- **Steps:** `GET /v1/history/devices/{device_id}/events` with `event_types` set to `motion.human,ding`, `ding`, `motion`, `motion.human`, `on_demand`, `bogus_type`, and with none.
- **Expected:** Only matching types; an error or empty result for an unknown type; `links.next` present, per the docs.
- **Actual:**
  - Every request returned `200` with the same 15 `on_demand` items, `bogus_type` included, and no `links`.
  - Items carry `relationships.cv_detections` and `meta.riid`, which aren't in the docs' field table.
  - Our own WHEP sessions created new `on_demand` items.
  - The simulated Package event created no `motion` or `ding` items.
  - Recorded as `tests/fixtures/ring/history_events_*.json`.
- **Severity:** Medium. A client that trusts the filter treats every item as a match.
- **Workaround:** The poller checks each page against the requested types and applies its own relevance rules. Demos use a manual trigger endpoint.
- **Suggestion:** Apply `event_types` in the Playground (or say it's unsupported there), return `400` for unknown types, document the extra fields, and have simulated events create history entries.

**4. Event History items don't carry the detection subtype, and the docs don't relate their IDs to webhook IDs**
- **Task:** Poll Event History as a webhook fallback, keep only human detections, and dedupe against webhooks for the same event.
- **Steps:** Read the Event History and Notifications → Motion Detection pages.
- **Expected:** Each item says which subtype matched, and the docs say whether a history `id` equals the webhook's `data.id`.
- **Actual:** You can filter by subtype (`motion.human`, `motion.vehicle`, `motion.other_motion`), but the documented item fields (`type`, `id`, `attributes.event_type`, `attributes.is_third_party_reviewed`, `attributes.start`, `attributes.end`, `relationships.source.data`) don't include the subtype. Both IDs are described only as "opaque — do not parse".
- **Severity:** Low.
- **Workaround:** Filter on a single subtype and take the subtype from the filter. Store Ring's event ID under a unique constraint, so the two paths dedupe if the IDs do match.
- **Suggestion:** Return the subtype on history items, and state whether the two IDs are the same value.

## Ring Developer Playground

**5. Playground tokens expire after 30 minutes; the docs don't say so and a 401 doesn't say why**
- **Task:** Record a demo with live captures over more than half an hour.
- **Steps:** Ran captures with a Playground one-click token. The next morning, triggered a capture with the old token. Then decoded a new token's JWT `iat`/`exp`.
- **Expected:** The lifetime stated in the docs or next to the token, and a 401 that says the token expired.
- **Actual:** `iat` 17:56:04 UTC, `exp` 18:26:04 UTC, so 30 minutes. The Authentication, Access Tokens and Refresh Tokens pages cover only OAuth tokens (about 4 hours, and about 30 days for refresh). The expired token got `401` with an empty body.
- **Severity:** Medium. Captures fail mid-session, and an app that reads the token at startup needs a restart for each new one.
- **Workaround:** A token provider that re-reads the token on every call, reads `exp` locally and refuses expired tokens with the exact expiry time. The dashboard shows the failure with a hint to paste a new token.
- **Suggestion:** Show the expiry next to the token, state the lifetime in the docs, explain 401s (e.g. `WWW-Authenticate: Bearer error="invalid_token"`), or offer longer-lived or refreshable dev tokens.

**6. No way to tell from the docs whether the Playground can deliver webhooks**
- **Task:** Receive `motion_detected` and `button_press` webhooks during Playground development.
- **Steps:** Searched the Ring knowledge MCP server ("Developer Playground webhooks simulate event", "playground simulate live view event package webhook").
- **Expected:** A statement of whether Playground events reach a webhook, and where to register the URL.
- **Actual:** No Playground page came back. The docs only describe a URL registered on an app ("Webhook URL is configured during partner onboarding"; "Webhook URL" under Account Linking). We found nowhere to give the Playground a URL.
- **Severity:** Medium. Ingest can't be tested end to end without a real device.
- **Workaround:** An Event History poller plus a manual trigger endpoint. The webhook path is tested with signed payloads built from the docs' examples.
- **Suggestion:** Let the Playground send signed test webhooks to a developer-supplied URL, and say whether simulated events appear in Event History.

**7. The live-view scene can't be chosen through the API, and the docs don't cover it**
- **Task:** Open a WHEP session on the simulated Package stream from a script.
- **Steps:** Generated a token, then `GET /v1/devices?include=status,capabilities` and `POST .../whep/sessions` on the only device returned.
- **Expected:** One device per scene, or a documented way to pick it.
- **Actual:** One device, `Playground Device`. It streamed a snowy bird feeder until the Package event was picked in the Playground UI (step 3, "Simulate live view event"). Knowledge MCP queries about Playground scenes returned `clarification_needed`.
- **Severity:** Medium. The demo scenario can't be scripted.
- **Workaround:** Pick the event by hand in the Playground tab that generated the token, then run the script.
- **Suggestion:** Document the scenes, expose them as devices or a parameter, and index the Playground guide in the knowledge MCP server.

**8. Playground steps stay locked in a new tab while a token is active**
- **Task:** Switch the simulated event from a second tab.
- **Steps:** Developer Console → Playground → "Try the Playground", while a token from another tab was still valid.
- **Expected:** Steps 2–3 usable, since the page knows a token is active.
- **Actual:** Step 1 shows "You have an active token… You can generate a new token in 27 minutes". Steps 2–3 show "Generate a token to get started" and are disabled.
- **Severity:** Medium. Closing that tab blocks live-view simulation for up to about 30 minutes.
- **Workaround:** Keep the original tab open and drive the scene from it.
- **Suggestion:** Unlock steps 2–3 in any tab while a token is active, or allow revoking and reissuing.

**9. Package stream length varies by session, and nothing signals the end of a clip**
- **Task:** Capture frames spread across the Package event.
- **Steps:** Selected the Package event, then opened three WHEP sessions over a few minutes.
- **Expected:** A predictable length, or an end signal (RTCP BYE, track `ended`, session closed).
- **Actual:** About 1.5 s, about 10 s and 26 s of video (the last one until our timeout). In the first two, RTP stopped while ICE/DTLS stayed `connected` and the device stayed `online: true`.
- **Severity:** Low to medium. A fixed-interval capture can come back nearly empty.
- **Workaround:** Decode until the stream stalls for 2 s, spread samples over what arrived, and stop early once the scene has settled.
- **Suggestion:** Document each event's duration and looping, and end the track or session when the clip finishes.

## Ring docs

**10. The docs give two different authorization-code lifetimes**
- **Task:** Check token lifetimes in the Ring docs (via the knowledge MCP server).
- **Steps:** Compared the token tables across pages.
- **Expected:** One lifetime for the OAuth authorization code.
- **Actual:** The Authentication overview and Access Tokens pages say 60 seconds; the Account Linking page says "10 minutes (single-use)".
- **Severity:** Low. A partner should design for 60 s, but the docs don't say which is right.
- **Workaround:** Exchange the code as soon as it arrives.
- **Suggestion:** Use one value in every token table.

## Amazon Bedrock

**11. A Bedrock API key is issued but denied on every call**
- **Task:** First Bedrock vision call (Converse with an image) using a Bedrock API key, us-east-1, on a new AWS Free-plan account.
- **Steps:** Generated a key in the console, set `AWS_BEARER_TOKEN_BEDROCK`, then called `ListInferenceProfiles` and `Converse` (boto3 1.43.113).
- **Expected:** The key works, or the console says it can't be used.
- **Actual:** `AccessDeniedException: ... is not authorized to perform: bedrock:CallWithBearerToken on resource: * with an explicit deny in a service control policy`. The console created the key and its IAM user without any warning.
- **Severity:** High. All Bedrock use with the key is blocked, and nothing inside the member account can override an SCP deny.
- **Workaround:** SigV4 credentials (IAM user or SSO), or an SCP exemption from the org admin.
- **Suggestion:** Check SCPs when a key is generated and warn on the key page, and name the blocked capability in plain words.

**12. Anthropic models work, then fail minutes later with "use case details have not been submitted"**
- **Task:** Text and image Converse calls to Claude models.
- **Steps:** `us.anthropic.claude-sonnet-4-6` and `us.anthropic.claude-haiku-4-5-20251001-v1:0` succeeded; about 3–5 minutes later, the same calls failed.
- **Expected:** Consistent access, blocked or allowed from the first call.
- **Actual:** `ResourceNotFoundException: Model use case details have not been submitted for this account. Fill out the Anthropic use case details form before using the model...`. A not-found error for a missing form looks like a wrong model ID.
- **Severity:** High. Every Anthropic model in the account was blocked.
- **Workaround:** Submit the Anthropic use-case form and wait up to 15 minutes.
- **Suggestion:** Gate access the same way from the first call, return `AccessDeniedException` with a link to the form, and show the requirement on the model's catalog page.

**13. Bedrock lists inference profiles the account can't invoke**
- **Task:** Pick a vision model for extraction.
- **Steps:** `ListInferenceProfiles(typeEquals=SYSTEM_DEFINED)` listed `us.anthropic.claude-sonnet-5-5`; then `Converse` on it. Then a 5-token probe across 26 listed `us.*` vision-capable profiles.
- **Expected:** A listed profile is invocable, or the listing says which aren't.
- **Actual:** `AccessDeniedException: anthropic.claude-sonnet-5-5 is not available for this account`. 13 of the 26 were denied (not available, or end-of-life/legacy).
- **Severity:** Medium. You find out by paying for a probe.
- **Workaround:** Probe each candidate with a tiny Converse call before trusting a model ID.
- **Suggestion:** Show per-account availability in `ListInferenceProfiles` and `ListFoundationModels`, or add a dry-run access check.

**14. boto3 reads Bedrock API keys only from an environment variable**
- **Task:** Load the Bedrock API key from app config (`.env` via pydantic-settings).
- **Steps:** Looked for a way to pass the key when creating the `bedrock-runtime` client.
- **Expected:** A client or session parameter for the key.
- **Actual:** botocore reads it only from `os.environ["AWS_BEARER_TOKEN_BEDROCK"]` at client creation.
- **Severity:** Low.
- **Workaround:** Export the configured key into the process environment before creating the client. A test confirms `Authorization: Bearer` is sent.
- **Suggestion:** Accept the token as a client or session parameter.

## MCP Python SDK and conformance suite

**15. The SDK answers an unknown tool with a tool result, not a protocol error**
- **Task:** MCP server on `mcp` 2.3.0 (`MCPServer`), checked against spec 2025-11-25.
- **Steps:** `tools/call` with `"name": "nope"` over Streamable HTTP.
- **Expected:** A JSON-RPC error. The tools page lists unknown tools as protocol errors, with the example `{"code": -32602, "message": "Unknown tool: invalid_tool_name"}`.
- **Actual:** `200` with `result: {"content": [{"type": "text", "text": "Unknown tool: nope"}], "isError": true}`.
- **Severity:** Low. Clients still see a failure, but a spec conformance check fails.
- **Workaround:** A server middleware checks the tool name first and raises `MCPError(INVALID_PARAMS, ...)`.
- **Suggestion:** Return `-32602` for unknown tools, or document why the SDK differs.

**16. In stateless Streamable HTTP, GET opens a stream that can never carry a message**
- **Task:** Same server with `stateless_http=True, json_response=True`.
- **Steps:** `GET /mcp` with `Accept: text/event-stream`.
- **Expected:** `405`, which the transport spec allows for a server with no SSE stream.
- **Actual:** The request didn't complete within 120 s and the probe had to be killed.
- **Severity:** Low. It ties up a connection per GET and hangs naive tests.
- **Workaround:** An ASGI wrapper answers GET and DELETE on `/mcp` with 405 (after the bearer check).
- **Suggestion:** Default to 405 for GET in stateless mode.

**17. A bad MCP-Protocol-Version gets the right status with a misleading message**
- **Task:** Test the rule that an unsupported `MCP-Protocol-Version` MUST get `400`.
- **Steps:** `tools/list` with `MCP-Protocol-Version: 1999-01-01`.
- **Expected:** `400` naming the unsupported version.
- **Actual:** `400`, with a message about missing `io.modelcontextprotocol/protocolVersion` and `clientCapabilities` `_meta` envelope keys. Those belong to the 2026-07-28 revision, which a 2025-11-25 client doesn't use.
- **Severity:** Low (debugging time).
- **Workaround:** None needed.
- **Suggestion:** Say "unsupported MCP-Protocol-Version '…'; supported: …".

**18. The MCP conformance suite can't test a server that requires a bearer token**
- **Task:** Run `@modelcontextprotocol/conformance` 0.1.16 against the Dockhand MCP server.
- **Steps:** `npx @modelcontextprotocol/conformance server --help`.
- **Expected:** An option to send a header or token.
- **Actual:** The only options are `--url`, `--scenario`, `--suite`, `--expected-failures`, `--output-dir`, `--spec-version` and `--verbose`, so every request goes out unauthenticated. Many scenarios also require the suite's own fixture tools (e.g. `test_simple_text`, `json_schema_2020_12_tool`), so an application server fails them by design.
- **Severity:** Medium for any server that follows the spec's "SHOULD implement proper authentication".
- **Workaround:** Serve the real app locally behind a wrapper that adds the token, and run only the scenarios that test a server's own surface (5/5 checks passed, `bench/results/mcp_conformance.json`).
- **Suggestion:** Add a `--header` option, and tag the scenarios that need the fixture server.
