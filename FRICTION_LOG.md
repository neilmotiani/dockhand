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
