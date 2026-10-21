# Friction log

Issues hit while building with the Ring API, Ring Developer Playground, Amazon Bedrock and the MCP SDK.

Format per entry: task, steps, expected vs. actual, severity, workaround, suggestion.

## 2026-10-15 · Bedrock API key accepted at creation but denied on every call

- **Task:** First Bedrock vision call (Converse with an image) using a Bedrock API key (`AWS_BEARER_TOKEN_BEDROCK`), region us-east-1, on a new AWS Free-plan account.
- **Steps:** Generated a Bedrock API key in the console. Set `AWS_BEARER_TOKEN_BEDROCK`. Called `ListInferenceProfiles` (bedrock) and `Converse` (bedrock-runtime) with boto3 1.43.113.
- **Expected:** The key works, or the console says it can't be used before it is issued.
- **Actual:** Both calls return `AccessDeniedException: User: arn:aws:iam::<account>:user/BedrockAPIKey-xxxx is not authorized to perform: bedrock:CallWithBearerToken on resource: * with an explicit deny in a service control policy`. The console created the key and its IAM user without warning that an organization SCP blocks bearer-token calls in this account.
- **Severity:** High (blocks all Bedrock usage with the key; no workaround inside the member account, because an explicit SCP deny overrides any IAM permission).
- **Workaround:** Use SigV4 credentials (IAM access key or IAM Identity Center/SSO) instead of the API key, or ask the org admin to exempt the account from the SCP.
- **Suggestion:** Check SCPs for `bedrock:CallWithBearerToken` when a key is generated and show a warning on the API key page. Name the blocked capability in plain words in the error ("Bedrock API keys are disabled by your organization").

## 2026-10-15 · Bedrock API keys are only read from an environment variable in boto3

- **Task:** Load the Bedrock API key from app config (`.env` via pydantic-settings).
- **Expected:** A client parameter such as `boto3.client("bedrock-runtime", api_key=...)`.
- **Actual:** botocore picks up the key only from `os.environ["AWS_BEARER_TOKEN_BEDROCK"]` at client creation, so apps that load config from files must export it into the process environment themselves.
- **Severity:** Low.
- **Workaround:** `make_bedrock_client()` exports the configured key before creating the client. A test intercepts the signed request to confirm `Authorization: Bearer` is sent.
- **Suggestion:** Accept the token as a client or session parameter.

## 2026-10-15 · Playground live-view scene can't be chosen through the API, and the docs don't cover it

- **Task:** WHEP spike: open a live-view session on the simulated Package stream from a server-side script (`scripts/ring_spike.py`).
- **Steps:** Generated a Playground token. Called `GET /v1/devices?include=status,capabilities`, then `POST .../whep/sessions` on the only device returned.
- **Expected:** One device per simulated scene (for example, a "Package" camera), or a documented way to pick the scene.
- **Actual:** The account has one device, `Playground Device` (AVC, online). Its WHEP stream showed a snowy bird feeder. The scene is picked in the Playground UI under step 3, "Simulate live view event". The Ring knowledge MCP server returns `clarification_needed` with no results for every Playground query I tried (simulated device, scenes, Package stream), and the API docs don't say how the Playground device chooses what it streams.
- **Severity:** Medium (the integration works, but there's no way to script the demo scenario or know in advance which footage an API client will get).
- **Workaround:** Select the event by hand in the Playground tab that generated the token, then run the script.
- **Suggestion:** Document the Playground's simulated scenes, expose them as separate devices or a query parameter, and index the Playground guide in the knowledge MCP server.

## 2026-10-15 · Playground steps stay locked in a new tab while a token is active

- **Task:** Open the Playground in a second tab to switch the simulated live-view event.
- **Steps:** Developer Console → Playground → "Try the Playground", while a token generated in another tab was still valid.
- **Expected:** Steps 2–3 (Explore APIs, Simulate live view event) usable, since the page knows a token is active.
- **Actual:** Step 1 shows "You have an active token… You can generate a new token in 27 minutes", but steps 2 and 3 show "Generate a token to get started" and are disabled. Until the cooldown ends, the only usable Playground is the original tab.
- **Severity:** Medium (closing or reloading that tab blocks live-view simulation for up to ~30 min).
- **Workaround:** Keep the tab that generated the token open, and drive the scene from it.
- **Suggestion:** Unlock steps 2–3 in any tab once a token is active, or let the developer revoke it and generate a new one.

## 2026-10-15 · WHEP Location header carries an undocumented `?location=` query, and DELETE's response isn't documented

- **Task:** Close a WHEP session with the URL from the `Location` header.
- **Expected (per the WHEP docs):** `Location: https://api.amazonvision.com/v1/devices/{device_id}/media/streaming/whep/sessions/{session_id}`.
- **Actual:** `.../whep/sessions/{uuid}?location=ava1.ring.whep.location.<~300-char token>`. A client that rebuilds the URL from `device_id` + `session_id` (as the docs' "Session URL Structure" suggests) drops it. Using the header unchanged, `DELETE` returned `200` with an empty body; the docs give no status code for DELETE. I haven't tested whether DELETE works without the query.
- **Severity:** Low.
- **Workaround:** Store the `Location` header verbatim and DELETE that exact URL (`session_url()` in `scripts/ring_spike.py`; covered by a test against the recorded fixture).
- **Suggestion:** Show the query parameter in the docs, tell clients to treat the Location URL as opaque, and document the DELETE response.

## 2026-10-15 · WHEP returns 201 and connects for an offline device, then sends no media

- **Task:** Re-run the WHEP spike after selecting the Package event in the Playground (step 3).
- **Steps:** `GET /v1/devices?include=status` → `Playground Device` with `online: false`. Then `POST .../whep/sessions` → 201 with a valid SDP answer. aiortc reached `connected` (ICE + DTLS).
- **Expected (per the WHEP docs):** `503 Device Unavailable` / "Device is currently offline".
- **Actual:** 201 Created and a connected peer, but `getStats()` showed zero inbound RTP packets over 20 s, so no frames. The device was online and streaming (bird-feeder scene) about 20 minutes earlier, before the Package event was selected. DELETE still returned 200. Recorded as `tests/fixtures/ring/devices_list_offline.json`.
- **Severity:** Medium (a client waits out its whole timeout instead of failing fast, and "connected" looks healthy).
- **Workaround:** `scripts/ring_spike.py` checks `online` from device discovery and exits before opening a session.
- **Suggestion:** Return the documented 503 for offline devices, and document how the Playground device's online state relates to the selected live-view event.

## 2026-10-15 · Playground Package stream length varies by session, and nothing signals the end of a clip

- **Task:** Capture frames spread across the Playground "Package" live-view event.
- **Steps:** Selected the Package event (Playground step 3), then opened three WHEP sessions over a few minutes.
- **Expected:** A predictable clip length, or a signal when the clip ends (RTCP BYE, track `ended`, session closed).
- **Actual:** Session 1 got ~1.5 s of video and session 2 got ~10 s (about 29 fps, 1518 RTP packets); both then stopped sending RTP while ICE/DTLS stayed `connected` and the device stayed `online: true`. Session 3 streamed for the full 26 s until our client timed out (772 frames). The picture is a static scene: one box on snowy steps, no driver or motion.
- **Severity:** Low–medium (a fixed-interval capture can silently come back nearly empty).
- **Workaround:** `scripts/ring_spike.py --spread` decodes until the stream stalls for 2 s (or the timeout), then keeps N frames evenly spaced over what actually arrived.
- **Suggestion:** Document each simulated event's duration and looping behaviour, and end the track or session when the clip finishes.

## 2026-10-15 · Bedrock lists inference profiles the account can't invoke

- **Task:** Bedrock vision spike with SigV4 credentials (IAM user, us-east-1), model `us.anthropic.claude-sonnet-5-5`.
- **Steps:** `ListInferenceProfiles(typeEquals=SYSTEM_DEFINED)` lists the profile, so `scripts/bedrock_spike.py` reports it "confirmed". Then `Converse`.
- **Expected:** A listed profile is invocable, or the listing marks which ones aren't available to the account.
- **Actual:** `AccessDeniedException: anthropic.claude-sonnet-5-5 is not available for this account`. A 5-token text probe across 26 listed `us.*` vision-capable profiles: 13 denied as "not available for this account" or end-of-life/legacy; Claude Sonnet 4.6 / 4.5, Opus 4.6 / 4.5, Haiku 4.5, Nova, Llama 4 and Pixtral succeeded.
- **Severity:** Medium (the listing API can't tell you which models you can actually use; you find out by paying for a probe).
- **Workaround:** Probe with a tiny Converse call before trusting a model ID.
- **Suggestion:** Add per-account availability to `ListInferenceProfiles` / `ListFoundationModels`, or a dry-run access check.

## 2026-10-15 · Anthropic models on Bedrock work, then fail minutes later with "use case details have not been submitted"

- **Task:** Same account, a few minutes after the probe above.
- **Steps:** Text-only `Converse` to `us.anthropic.claude-sonnet-4-6` and `us.anthropic.claude-haiku-4-5-20251001-v1:0` succeeded. About 3–5 minutes later, the same calls (text and image) on both models failed.
- **Expected:** Consistent access: either blocked from the first call, or allowed throughout.
- **Actual:** `ResourceNotFoundException: Model use case details have not been submitted for this account. Fill out the Anthropic use case details form before using the model. If you have already filled out the form, try again in 15 minutes.` Using a not-found exception for a missing form makes it look like a wrong model ID.
- **Severity:** High (blocks every Anthropic model in the account).
- **Workaround:** Submit the Anthropic use case form in the Bedrock console and wait up to 15 minutes.
- **Suggestion:** Gate access the same way from the first call; return `AccessDeniedException` with a link to the form; show the form requirement on the model's catalog page before any invoke.

## 2026-10-16 · No way to tell from the docs whether the Playground can deliver webhooks

- **Task:** Receive `motion_detected` / `button_press` webhooks while developing against the Developer Playground.
- **Steps:** Searched the Ring knowledge MCP server for Playground + webhook ("Developer Playground webhooks simulate event", "playground simulate live view event package webhook").
- **Expected:** A statement of whether Playground events reach a webhook, and if so where to register the URL.
- **Actual:** Neither search returned a Playground page. The webhook docs only describe a URL registered on an app: Notifications says "Webhook URL is configured during partner onboarding", and the Configure page lists "Webhook URL" under an app's Account Linking settings. We found no place to give the Playground a webhook URL.
- **Severity:** Medium (the ingest path can't be tested end to end without a real device and a linked account).
- **Workaround:** Event History poller (`app/ingest/poller.py`) plus a manual trigger endpoint (`POST /v1/trigger`). The webhook path is tested with signed payloads built from the docs' examples.
- **Suggestion:** Let the Playground send signed test webhooks to a developer-supplied URL (like Stripe's "send test webhook"), and say on the Playground page whether simulated events appear in Event History.

## 2026-10-16 · Event History items don't carry the detection subtype, and the docs don't relate their IDs to webhook IDs

- **Task:** Poll Event History as a webhook fallback, keep only human detections, and dedupe against webhooks for the same event.
- **Steps:** Read Event History and Notifications → Motion Detection.
- **Expected:** Each history item says which subtype it was, and the docs say whether a history item's `id` is the same as the webhook's `data.id` for the same event.
- **Actual:** Event History lets you *filter* by subtype ("`motion.human`, `motion.vehicle`, `motion.other_motion`"), but the documented response fields for an item are only `type`, `id`, `attributes.event_type`, `attributes.is_third_party_reviewed`, `attributes.start`, `attributes.end` and `relationships.source.data`, so a returned motion item doesn't say which subtype matched. Both pages describe the ID only as opaque: Event History's `data[].id` is a "Directed event ID (opaque — do not parse)", and the webhook's `data.id` is a "Unique identifier for this specific motion event (opaque — do not parse)". Neither page says whether they're the same value.
- **Severity:** Low.
- **Workaround:** Query with a filter that pins one subtype (`motion.human,ding`) and take the subtype from the filter. Store Ring's event ID under a unique constraint, so if the IDs match, a webhook and a poll of the same event dedupe.
- **Suggestion:** Return the subtype on history items, and state whether the history event ID equals the webhook `data.id`.

## 2026-10-16 · Event History ignores `event_types` in the Playground, and items carry undocumented fields

- **Task:** Record real Event History responses for the polling fallback.
- **Steps:** `GET /v1/history/devices/{device_id}/events` on the Playground Device with `event_types` set to `motion.human,ding`, `ding`, `motion`, `motion.human`, `on_demand`, `bogus_type`, and with no filter.
- **Expected (per the Event History docs):** only matching types; an error (or empty result) for an unknown type; `links.next` present ("`links.next` may be present even when `data` is empty").
- **Actual:** Every request returned `200` with the same 15 `on_demand` items, including `event_types=bogus_type`. No response had `links`. Items also carry `relationships.cv_detections` (`{"data": []}`) and `meta.riid` (`null`), which the docs' response fields table doesn't list. After two WHEP sessions of ours, two new `on_demand` items appeared with matching times (12:49:48 and 12:50:28 PT), which fits the docs' "User or partner initiated a live view session". With the Package event running, no `motion` or `ding` items appeared. Recorded as `tests/fixtures/ring/history_events_*.json`.
- **Severity:** Medium. A client that trusts the filter would treat every returned item as a match. Ours would have labelled any motion item `human`.
- **Workaround:** The poller checks each page against the requested types, and only infers a subtype when the page is consistent with the filter. It enqueues only items that pass its own relevance rules. For Playground demos, use `POST /v1/trigger`.
- **Suggestion:** Apply `event_types` in the Playground (or say it's unsupported there), return `400` for unknown types, document `cv_detections` and `riid`, and have simulated events (Package) create history entries.

## 2026-10-17 · Playground tokens expire after 30 minutes; the docs don't say so and a 401 doesn't say why

- **Task:** Record the demo: trigger live captures from the dashboard over a session longer than half an hour.
- **Steps:** Generated a Playground one-click token, put it in `RING_ACCESS_TOKEN` and ran captures. The next morning, triggered a capture with the previous token still configured. Then generated a new token and decoded its JWT payload (without printing the token) to read `iat` and `exp`.
- **Expected:** The token's lifetime stated in the docs or next to the token, and a 401 that says the token expired.
- **Actual:** The new token's claims give `iat` 17:56:04 UTC and `exp` 18:26:04 UTC: a 30-minute lifetime. The Authentication, Access Tokens and Refresh Tokens pages describe OAuth access tokens (~4 hours, with `expires_in` in the token response) and refresh tokens (~30 days). None of them mentions the Playground token's lifetime. The expired token's `GET /v1/devices` returned `401` with an empty body.
- **Severity:** Medium. Every capture fails once the token lapses mid-session. An app that loads the token at startup also needs a restart for each new one, which takes longer than a demo take.
- **Workaround:** `PlaygroundTokenProvider` re-reads `RING_ACCESS_TOKEN` from `.env` on every Ring call, so a fresh token works without a restart. It reads `exp` from the token and refuses an expired one with the exact expiry time instead of calling Ring. After a 401 it won't reuse that token until a different one is pasted. The dashboard's activity list shows the failure with a hint to paste a fresh token.
- **Suggestion:** Show the expiry time next to the token in the Playground. State the Playground token lifetime in the docs. Put a reason in the 401 body or a `WWW-Authenticate` header (for example `error="invalid_token", error_description="expired"`). Or offer a refresh path or longer-lived tokens for Playground development.

## 2026-10-17 · The docs give two different authorization-code lifetimes

- **Task:** Check token lifetimes in the Ring docs (Ring knowledge MCP server) while writing the entry above.
- **Steps:** Searched the docs for token lifetimes and compared the token tables.
- **Expected:** One lifetime for the OAuth authorization code.
- **Actual:** The Authentication overview and Access Tokens pages say 60 seconds ("One-time use; exchanged for access and refresh tokens"). The Account Linking page's token table says "10 minutes (single-use)".
- **Severity:** Low for this project (we use Playground tokens). For a partner implementing the token exchange, the 60-second figure is the one to design for, but the docs don't say which is right.
- **Workaround:** None needed yet. When the OAuth provider is built, exchange the code immediately on receipt.
- **Suggestion:** Use one value in every token table.

## 2026-10-18 · MCP Python SDK answers an unknown tool with a tool result, not a protocol error

- **Task:** MCP server on the official Python SDK (`mcp` 2.3.0, `MCPServer`), checked against the 2025-11-25 spec.
- **Steps:** `tools/call` with `"name": "nope"` over Streamable HTTP.
- **Expected:** A JSON-RPC error. The spec's tools page lists "Unknown tools" under protocol errors, and its example is `{"code": -32602, "message": "Unknown tool: invalid_tool_name"}`.
- **Actual:** `200` with `result: {"content": [{"type": "text", "text": "Unknown tool: nope"}], "isError": true}`.
- **Severity:** Low. Clients still see a failure, but a conformance check against the spec text fails.
- **Workaround:** A server middleware checks the tool name before dispatch and raises `MCPError(INVALID_PARAMS, "Unknown tool: …")` (`mcp_server/server.py`).
- **Suggestion:** Return `-32602` for unknown tools by default, or say in the SDK docs why it differs from the spec.

## 2026-10-18 · Stateless Streamable HTTP: GET opens a stream that can never carry a message

- **Task:** Same server, `streamable_http_app(stateless_http=True, json_response=True)`.
- **Steps:** `GET /mcp` with `Accept: text/event-stream` from an httpx ASGI client.
- **Expected:** `405 Method Not Allowed`. The transport spec allows that for a server that offers no SSE stream, and a stateless server has no session for a stream to belong to.
- **Actual:** The request didn't complete within 120 s; the probe script had to be killed.
- **Severity:** Low. It ties up a connection per GET and hangs naive tests.
- **Workaround:** An ASGI wrapper answers GET and DELETE on `/mcp` with 405 (after the bearer-token check).
- **Suggestion:** Default to 405 for GET in stateless mode.

## 2026-10-18 · Bad MCP-Protocol-Version: right status, misleading message

- **Task:** Conformance test for "an invalid or unsupported MCP-Protocol-Version MUST get 400 Bad Request".
- **Steps:** `tools/list` with `MCP-Protocol-Version: 1999-01-01`.
- **Expected:** `400` naming the unsupported version.
- **Actual:** `400` (correct), with `"params._meta must be an object carrying the required 'io.modelcontextprotocol/protocolVersion' and 'io.modelcontextprotocol/clientCapabilities' envelope keys"`. That message is about the 2026-07-28 per-request envelope, which a 2025-11-25 client has never heard of.
- **Severity:** Low (debugging time).
- **Workaround:** None needed.
- **Suggestion:** Say "unsupported MCP-Protocol-Version '1999-01-01'; supported: …" when the header names no known version.

## 2026-10-18 · MCP conformance suite can't test a server that requires a bearer token

- **Task:** Run `@modelcontextprotocol/conformance` 0.1.16 (`conformance server --url …`) against the Dockhand MCP server, which requires `Authorization: Bearer`.
- **Steps:** `npx @modelcontextprotocol/conformance server --help`.
- **Expected:** An option to pass a header or token.
- **Actual:** Options are `--url`, `--scenario`, `--suite`, `--expected-failures`, `--output-dir`, `--spec-version`, `--verbose`; every request goes out unauthenticated. Many scenarios also need the suite's own fixture tools (`test_simple_text`, `json_schema_2020_12_tool`, …), so an application server fails them by design.
- **Severity:** Medium for anyone who follows the spec's "SHOULD implement proper authentication".
- **Workaround:** `scripts/mcp_conformance.py` serves the real app on 127.0.0.1 behind a wrapper that adds the token, and runs only the scenarios that test a server's own surface.
- **Suggestion:** A `--header` option, and a tag on scenarios that need the fixture server.
