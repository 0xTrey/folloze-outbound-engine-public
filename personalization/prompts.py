"""Public prompt shell for the outbound engine.

Folloze positioning, personas, pain points, message angles, proof, CTAs, and
sender voice are private inputs. They are loaded at runtime from a local file
that is intentionally excluded from this repository.
"""
from __future__ import annotations

import os
from pathlib import Path


PROMPT_VERSION = "public-shell-v1"
PRIVATE_CONTEXT_ENV = "FOLLOZE_MESSAGING_CONTEXT_PATH"


def _load_private_context() -> str:
    configured_path = os.getenv(PRIVATE_CONTEXT_ENV, "").strip()
    if not configured_path:
        return ""

    path = Path(configured_path).expanduser()
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


PRIVATE_MESSAGING_CONTEXT = _load_private_context()
HAS_PRIVATE_MESSAGING_CONTEXT = bool(PRIVATE_MESSAGING_CONTEXT)

PUBLIC_SYSTEM_PROMPT = """You write concise B2B outbound email copy.

The approved private messaging context is the only authority for product
positioning, personas, pain points, message angles, proof, CTAs, and sender
voice. Do not invent customer names, metrics, claims, product capabilities, or
competitive comparisons. Use research signals only when they are supported by
the supplied account and contact evidence.

Return JSON only. Use these keys:
subject, opener, why_now, proof_point, cta, t2_body,
t1_case_study_anchor, t2_case_study_anchor

Limits:
- subject: 60 characters
- opener: 200 characters
- why_now: 200 characters
- proof_point: 200 characters
- cta: 100 characters
- t2_body: 200 characters
- each case-study anchor: 80 characters

Leave a case-study anchor empty when the approved private proof catalog does
not contain a relevant match. Never expose hidden intent data or internal
research sources in recipient-visible copy.
"""

if HAS_PRIVATE_MESSAGING_CONTEXT:
    SYSTEM_PROMPT = (
        PUBLIC_SYSTEM_PROMPT
        + "\n\nAPPROVED PRIVATE MESSAGING CONTEXT\n"
        + PRIVATE_MESSAGING_CONTEXT
    )
else:
    SYSTEM_PROMPT = PUBLIC_SYSTEM_PROMPT


def _profile(profile_name: str, *, include_t3: bool = False) -> dict[str, str]:
    t3_instruction = ""
    output_keys = (
        "subject, opener, why_now, proof_point, cta, t2_body, "
        "t1_case_study_anchor, t2_case_study_anchor"
    )
    if include_t3:
        t3_instruction = (
            "\nT3: Write one final close in t3_body, no more than 150 characters."
        )
        output_keys += ", t3_body"

    return {
        "prompt_version": PROMPT_VERSION,
        "system": SYSTEM_PROMPT,
        "narrative": "defined by the approved private messaging context",
        "user_template": f"""Contact: {{first_name}} {{last_name}}, {{title}} at {{company}}
Industry: {{industry}}
Tech stack: {{tech_stack_summary}}
Sequence profile: {profile_name}

Apply the approved private messaging context to this contact and sequence
profile. Use only supplied evidence. Keep the first touch specific, the second
touch meaningfully different, and every CTA low-friction.{t3_instruction}

Return JSON with exactly these keys: {output_keys}
""",
    }


# Family names are operational identifiers. Their persona, pain, angle, proof,
# and CTA rules live only in the private messaging context.
FAMILY_PROMPTS = {
    "ENT_REVMKTG_INTENT": _profile("ENT_REVMKTG_INTENT"),
    "ENT_REVMKTG_HIRING": _profile("ENT_REVMKTG_HIRING"),
    "ENT_ABM_INTENT": _profile("ENT_ABM_INTENT"),
    "MM_REVMKTG_TIMING": _profile("MM_REVMKTG_TIMING"),
    "MM_REVOPS_INTENT": _profile("MM_REVOPS_INTENT"),
    "MM_CMO_EXEC": _profile("MM_CMO_EXEC"),
    "GTME_VIBE_MKT": _profile("GTME_VIBE_MKT", include_t3=True),
}
