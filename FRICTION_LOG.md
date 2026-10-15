# Friction log

Issues hit while building with the Ring API, Ring Developer Playground, Amazon Bedrock and the MCP SDK.

Format per entry: task, steps, expected vs. actual, severity, workaround, suggestion.

## 2026-10-15 · Bedrock API key accepted at creation but denied on every call

- **Task:** First Bedrock vision call (Converse with an image) using a Bedrock API key (`AWS_BEARER_TOKEN_BEDROCK`), region us-east-1.
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
