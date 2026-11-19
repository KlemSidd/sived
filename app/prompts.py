import hashlib

PRECEDENCE = """Platform precedence, which wins over the persona and loses to the skill's canon rules:
File writes in the skill are void. The platform stores the documents.
Do not rewrite the manuscript, draft replacement prose, or line-edit.
The Diagnose ceiling in the prompt is 400 words. Do not write longer than that ceiling.
LOCKED is only for the do-not-change box and an explicit author answer. Manuscript facts are DRAFT or OPEN.
One ask is in this call. Do not cover the other asks.
"""

ASKS = {
    "promise": "Diagnose promise and genre fit. What the opening promises the named audience, and where the book breaks that promise.",
    "plot": "Diagnose plot and structure. Causality, turns, midpoint, climax, ending. Name the breaks.",
    "character": "Diagnose character. Protagonist want, opposition, and arcs that do not change the ending.",
    "stakes": "Diagnose stakes and pacing. Where the book goes slack, what the reader is asked to care about, and scenes that do not change a later page.",
    "focus": "Diagnose only the author's focus, as a separate pass. Do not replace the other diagnoses.",
    "readback": (
        "Summarize this run into compendium section order. Tag each line LOCKED, DRAFT, or OPEN. "
        "Do not write a file. Do not rewrite the manuscript."
    ),
}


def build_prompt(
    *,
    skill: str,
    ask: str,
    persona: str,
    manuscript: str,
    form: str,
    attachment: str = "",
    skill_body: str | None = None,
    question: str | None = None,
) -> str:
    question_text = question if question is not None else ASKS[ask]
    parts = []
    if skill_body:
        parts.append(skill_body)
    parts.extend(
        [
            PRECEDENCE,
            f"Persona emphasis (cannot override the skill):\n{persona}".strip(),
            form,
            f"This call's ask:\n{question_text}",
        ]
    )
    if attachment:
        parts.append("Compendium already stored. Treat LOCKED lines as constraints. Do not dump it back.\n" + attachment)
    parts.append("Manuscript:\n" + manuscript)
    return "\n\n".join(parts)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def visible_words(text: str) -> int:
    return len(text.split())
