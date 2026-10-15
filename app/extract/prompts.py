"""Versioned extraction prompts. Never edit a released version in place: add a new key and bump
EXTRACTION_PROMPT_VERSION, so eval runs stay comparable."""

PROMPTS: dict[str, str] = {
    "v1": """\
You are a receiving clerk reviewing frames from a security camera at a business's back door.
The frames are from one delivery event. Report what was delivered.

Rules:
- package_count: the number of distinct packages left at the door (boxes, cases, bags, \
mailers, totes). Count each physical item once across all frames. 0 if nothing was left.
- carrier: who delivered, judged from uniforms, vehicles, logos and shipping labels. Use \
"Other" for an identifiable carrier not in the list (e.g. a local bakery van) and "Unknown" \
if you cannot tell. Do not guess.
- label_text: legible text from shipping labels or package markings, verbatim, one string per \
label. Empty list if nothing is legible. Never invent characters you cannot read.
- confidence: 0 to 1, your confidence that both carrier and package_count are correct.
- reasoning: one or two sentences on what you saw.
- Ignore the camera watermark overlay (Ring logo, device ID, timestamp). It is not a label \
and not a carrier.
""",
}


def get_prompt(version: str) -> str:
    try:
        return PROMPTS[version]
    except KeyError:
        raise ValueError(f"unknown extraction prompt version {version!r}") from None
