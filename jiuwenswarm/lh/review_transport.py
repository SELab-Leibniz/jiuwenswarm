# Copyright (c) 2026 Chrys. All rights reserved.

"""Normalize native Reviewer transport before Core's strict decision parser.

Core deliberately accepts only a whole-output JSON decision or one complete
JSON fence. Some native Reviewers instead write a human-readable review and
append the same decision as a trailing JSON object. Core then treats the
initial decision as malformed and asks for a bounded format repair. The repair
itself is valid, but Core's consistency guard rejects a repaired ``complete``
verdict unless the human-readable review already ends with ``PACT_COMPLETE``.

This module repairs that transport mismatch without inventing a verdict: when
the trailing object is an unambiguous complete decision with an allowed
schema, the review is plain body text (not a quotation or code), and nothing in
the body conflicts with completion, the matching terminal marker is appended
only if the review does not already end with one. An empty structured gap block
(``[]``, whitespace, or ``null``) means "no gaps" and is not a conflict. The
decision body itself is never rewritten or extracted. Anything else keeps the
ordinary format-repair and consistency-guard path.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pact_core.review_decision import parse_review_decision

_COMPLETE = "complete"
_COMPLETE_MARKER = "PACT_COMPLETE"

# Common diagnostic metadata a Reviewer may attach to the decision object.
# Unknown fields are still rejected so a genuinely different schema keeps the
# ordinary repair path, but these do not change the verdict's meaning.
_ALLOWED_DECISION_FIELDS = {
    "verdict",
    "plan_challenge",
    "reason",
    "summary",
    "notes",
    "note",
    "explanation",
    "detail",
    "details",
    "rationale",
    "message",
    "comment",
    "comments",
    "observations",
    "evidence",
    "findings",
    "recommendations",
    "recommendation",
    "severity",
    "confidence",
    "status",
    "state",
    "result",
    "outcome",
    "disposition",
}

# Fields whose value says more than their name: a complete verdict alongside a
# failed/blocked/rejected status is still a conflicting review.
_OUTCOME_FIELDS = {"status", "state", "result", "outcome", "disposition"}
_NEGATIVE_OUTCOME = re.compile(
    r"(?i)\b(?:fail(?:ed|ure|s)?|blocked|reject(?:ed|ion|s)?|incomplete|"
    r"not[\s_-]+complete|unresolved|unmet|unsatisfied|aborted|denied)\b"
)

# A completion marker preceded by an explicit negation is not an assertion of
# completion. This is separate from the narrow line-start marker rule below so
# a negated marker in prose also conflicts with a trailing complete decision.
_NEGATED_COMPLETE = re.compile(
    r"(?i)\b(?:not|no|never|without|isn'?t|aren'?t|wasn'?t|weren'?t|"
    r"don'?t|doesn'?t|didn'?t|cannot|can'?t|won'?t)\s+"
    r"(?:(?:a|an|the|any)\s+)?PACT_COMPLETE\b"
)

# A whole line that is only a negative outcome word, after removing markdown
# emphasis and punctuation. Ordinary prose such as "wrong secret rejected" or
# "Blocking gaps: none." must not match.
_NEGATIVE_CONCLUSION_LABELS = {
    "ABORTED",
    "BLOCK",
    "BLOCKED",
    "DENIED",
    "FAIL",
    "FAILED",
    "FAILURE",
    "INCOMPLETE",
    "NOT ACCEPTED",
    "NOT APPROVED",
    "NOT COMPLETE",
    "NOT DONE",
    "NOT PASSED",
    "NOT READY",
    "REJECT",
    "REJECTED",
    "REJECTION",
    "UNMET",
    "UNRESOLVED",
    "UNSATISFIED",
    "UNSUCCESSFUL",
}

# Terminal markers other than the completion marker itself. Core only defines
# COMPLETE/FAILED, but a review may use BLOCKED or a future terminal marker.
_CONFLICT_MARKER = re.compile(r"\bPACT_(?:FAILED|BLOCKED|REJECTED|INCOMPLETE)\b")
_GAP_BLOCK = re.compile(
    r"<pact-gaps\b[^>]*>\s*(.*?)\s*</pact-gaps\s*>",
    re.DOTALL | re.IGNORECASE,
)
_SELF_CLOSING_GAP_BLOCK = re.compile(r"<pact-gaps\b[^>]*/\s*>", re.IGNORECASE)
_EMPTY_GAPS_BEFORE_COMPLETE = re.compile(
    r"(?m)^[ ]{0,3}(<pact-gaps>[ \t]*\[[ \t]*\][ \t]*</pact-gaps>)[ \t]*\n"
    r"(?:[ \t]*\n)*[ ]{0,3}PACT_COMPLETE[ \t]*\Z"
)

# Positive statements that happen to contain words also used for gaps/failures.
# Remove them before looking for explicit rejection or gap signals.
_NEGATED_CONFLICT = re.compile(
    r"(?i)\b(?:no|zero|without|free\s+of)\s+"
    r"(?:(?:remaining|unresolved|open|outstanding|blocking|material|significant)\s+)*"
    r"(?:gaps?|issues?|failures?|defects?|problems?|rejections?|blockers?)\b"
)
_GAP_NONE = re.compile(
    r"(?i)\b(?:(?:remaining|unresolved|open|outstanding|blocking|blocked|material|significant)\s+)?"
    r"gaps?\s*(?:(?:are|is|were|was)\s+)?(?:[:=-]\s*)?(?:none|nil|n/a|zero|0)\b"
)
_EMPHASIZED_EMPTY_GAP = re.compile(
    r"(?im)^[ \t]*(?P<em>\*\*|__)(?:(?:remaining|open|blocking|unresolved)[ \t]+)?"
    r"gaps?:(?P=em)[ \t]+(?:none|nil|zero|0)[ \t]*\.?"
    r"(?=[ \t]*$|[ \t]+(?:the[ \t]+)?(?:prior|previous|earlier)[ \t]+"
    r"[^.!?\n:]{0,65}\b(?:is|are|was|were|has been|have been)[ \t]+(?:fixed|resolved|corrected)\b)"
)
# An explicitly tested input list may name a refusal as an exercised behavior.
# Limit that exception to a named pair of inputs; retain refusals of the review.
_TESTED_INPUT_LIST = re.compile(
    r"(?im)^[^\n:]{0,100}\b(?:coverage|checks|tests|cases)\b[^\n:.!?]{0,50}\b(?:all|both|each)\b"
    r"[^\n:.!?]{0,50}\b(?:tested|verified|passed|exercised)\b[^\n:.!?]{0,8}:[^\n]*"
)
_INPUT_PAIR_REFUSAL = re.compile(
    r"(?i)(?:[:,][ \t]+|\band[ \t]+)(?P<inputs>[a-z][\w-]*/[a-z][\w-]*)[ \t]+refusal"
    r"(?=,[ \t]+[a-z][\w-]*(?:/[a-z][\w-]*)*[ \t]+(?:validation|handling|checks?|bounds?|coverage)\b)"
)
_REVIEW_OBJECT = re.compile(
    r"(?i)\b(?:accept\w*|approv\w*|review\w*|candidate|implementation|submission|patch|release|merge|"
    r"delivery|deployment|promotion|"
    r"sign[-_]?off|verdict|work|test|checks?|verification)\b"
)
_UNRESOLVED_REVIEW = re.compile(
    r"(?i)\b(?:missing|absent)\s+(?:the\s+)?(?:required|expected|mandatory)\s+"
    r"(?:[\w-]+\s+){0,3}(?:outputs?|artifacts?|files?)\b"
    r"|\b(?:required|expected|mandatory)\s+(?:[\w-]+\s+){0,3}(?:outputs?|artifacts?|files?)\s+"
    r"(?:(?:is|are|was|were|remains?)\s+)?(?:still\s+)?(?:missing|absent)\b"
    r"|\b(?:candidate|submission|release|deployment|delivery|promotion|change)\s+"
    r"(?:is|remains|has been)\s+(?:still\s+)?(?:held|pending|on hold|withheld)\b"
    r"|\b(?:open|unresolved|outstanding)\s+(?:questions?|concerns?)\b"
    r"|\b(?:uncertain|unsure|not sure)\s+(?:whether|if)\b"
)


_NO_UNMET = re.compile(
    r"(?i)\b(?:no|zero)\s+(?:[a-z-]+\s+){0,8}"
    r"(?:unmet|missing|incomplete|unsatisfied|failing|failed|broken)\b"
)
_NOTHING_WRONG = re.compile(
    r"(?i)\b(?:nothing|none)\s+(?:(?:is|are|was|were)\s+)?"
    r"(?:missing|wrong|broken|unresolved|incomplete|blocking|failing)\b"
)
_NEGATED_DEFECT = re.compile(
    r"(?i)\b(?:is|are|was|were)\s+not\s+"
    r"(?:missing|incomplete|unmet|unsatisfied|failing|failed|broken|a\s+(?:gap|failure|defect|problem))\b"
)
# A negated success predicate is a contradiction even when the auxiliary is
# past tense ("did not pass", "was not completed") or contracted ("wasn't
# verified"). It must remain separate from the affirmative exemption above.
_NEGATED_SUCCESS = re.compile(
    r"(?i)(?:"
    # "Not all checks pass." needs the intervening noun: an adverb-only
    # extension after the negation cannot match this quantifier form.
    r"\bnot\s+all\s+(?:[a-z][a-z'-]*\s+){1,4}?pass(?:ed|es)?\b"
    r"|"
    r"\b(?:"
    r"(?:do|does|did|was|were|is|are|has|have|had|must|should|could|would|will|can)"
    r"\s+not"
    r"|do(?:es)?n'?t|didn'?t|wasn'?t|weren'?t|isn'?t|aren'?t|hasn'?t|haven'?t|hadn'?t"
    r"|mustn'?t|shouldn'?t|couldn'?t|wouldn'?t|won'?t|can'?t|cannot"
    r")\s+"
    # Repeated qualifiers may be interposed before or after "been": "has not
    # yet been run", "was not fully verified", "did not actually pass".
    r"(?:(?:yet|still|all|fully|actually|both|every)\s+)*"
    r"(?:been\s+)?"
    r"(?:(?:yet|still|all|fully|actually|both|every)\s+)*"
    r"(?:pass(?:ed|es|ing)?|succeed(?:ed|s|ing)?|successful(?:ly)?|"
    r"complet(?:e|ed|es|ing)|verif(?:y|ied|ies|ying)|run|runs|ran|running)\b"
    r"|"
    # "never" is restricted to result predicates; an unrun verification is
    # detected separately by _UNRUN_VERIFICATION.
    r"\bnever\s+"
    r"(?:(?:yet|still|all|fully|actually|both|every)\s+)*"
    r"(?:been\s+)?"
    r"(?:(?:yet|still|all|fully|actually|both|every)\s+)*"
    r"(?:pass(?:ed|es|ing)?|succeed(?:ed|s|ing)?|successful(?:ly)?|"
    r"complet(?:e|ed|es|ing)|verif(?:y|ied|ies|ying))\b"
    r")"
)

# Explicit rejection or an unfinished-gap declaration outside the trailing
# verdict. Patterns are deliberately narrow: a false negative only preserves
# the original repair path, while a false positive could hide completion.
_CONFLICT_PATTERNS = (
    # A rejection of this candidate/work, not a technical phrase such as
    # "the request was rejected with 400" or "wrong secret rejected".
    re.compile(
        r"(?i)\b(?:I|we)\s+(?:hereby\s+|must\s+|do\s+)?reject\b"
        r"|\b(?:reject|rejects|rejecting)\s+(?:this|the)\s+"
        r"(?:candidate|work|implementation|submission|change|patch|result|review)\b"
        r"|\b(?:candidate|work|implementation|submission|change|patch|result|review)\s+"
        r"(?:is|was|has\s+been|had\s+been|must\s+be|should\s+be|will\s+be)\s+"
        r"(?:rejected|refused)\b"
    ),
    re.compile(r"(?i)\b(?:refuse|refuses|refused|refusing|refusal)\b"),
    re.compile(
        r"(?i)\b(?:do not|don't|does not|doesn't|must not|mustn't|should not|shouldn't|"
        r"cannot|can't|will not|won't)\s+(?:be\s+)?"
        r"(?:accept(?:ed)?|approv(?:e|ed)|deliver(?:ed)?|complete(?:d)?|pass(?:ed)?)\b"
    ),
    _NEGATED_SUCCESS,
    re.compile(r"(?i)\bnot\s+(?:acceptable|approved|complete|completed|ready|passing)\b"),
    re.compile(
        r"(?i)\b(?:request(?:s|ed|ing)?|require(?:s|d|ing)?|need(?:s|ed|ing)?)\s+"
        r"(?:further\s+|more\s+)?(?:changes?|rework|repairs?|fixes?)\b"
    ),
    re.compile(r"(?i)\b(?:changes?|rework|repairs?|fixes?)\s+(?:are|is)\s+(?:required|needed|necessary)\b"),
    re.compile(
        r"(?i)\b(?:return|send|hand)\s+(?:it|this|candidate|work|the\s+candidate)?\s*"
        r"back\s+to\s+(?:the\s+)?(?:worker|implementation)\b"
    ),
    re.compile(
        r"(?i)\b(?:still|remaining|unresolved|open|outstanding|blocking|blocked)\s+"
        r"(?:[a-z-]+\s+){0,3}gaps?\b"
    ),
    re.compile(r"(?i)\bgaps?\s+(?:remain|remains|are|is|exist|exists|found|identified|listed|present)\b"),
    re.compile(r"(?im)^\s*(?:remaining\s+|open\s+|unresolved\s+)?gaps?\s*:"),
    re.compile(
        r"(?i)\b(?:missing|incomplete|unimplemented|unmet|unsatisfied)\s+"
        r"(?:required\s+)?(?:behavior|behaviour|functionality|implementation|features?|"
        r"coverage|tests?|requirements?|criteria|criterion|work|support)\b"
    ),
    re.compile(r"(?i)\b(?:candidate|work|implementation|review)\s+(?:is|remains)\s+(?:incomplete|broken|failing)\b"),
    re.compile(r"(?i)\bnot\s+(?:implemented|satisfied|met|addressed|covered|supported)\b"),
    re.compile(r"(?i)\b(?:does|do|did)\s+not\s+(?:meet|satisfy|implement|cover|support)\b"),
    re.compile(r"(?i)\b(?:fails?|failing|failed)\s+(?:to\s+)?(?:meet|satisfy|implement|cover|support|pass)\b"),
    re.compile(
        r"(?i)\b(?:fails?|failing|failed)\s+(?:the\s+)?"
        r"(?:acceptance|tests?|checks?|criteria|criterion|requirements?|task)\b"
    ),
    re.compile(r"(?i)\b(?:blocking|unresolved)\s+(?:issues?|problems?|defects?|failures?|questions?)\b"),
    # A labelled conclusion line states the review's own verdict. Keep this
    # line-anchored so technical evidence such as "the request was rejected
    # with 400" or "wrong secret rejected" remains ordinary prose.
    re.compile(
        r"(?im)^[ \t]*(?:>[ \t]*)?"
        r"(?:#{1,6}[ \t]+|[-*+][ \t]+|\d+[.)][ \t]+)?"
        r"(?:[*_~`]+[ \t]*)*"
        r"(?:(?:[A-Za-z][A-Za-z'’.-]*)[ \t]+){0,3}"
        r"(?:[*_~`]+[ \t]*)*"
        r"(?:verdict|status|result|outcome|decision)"
        r"[ \t]*(?:[*_~`]+[ \t]*)*:[ \t]*(?:[*_~`]+[ \t]*)*"
        r"(?:rejected|failed|blocked|incomplete|not[ \t]+complete)"
        r"[.!?,;:]*[ \t]*(?:[*_~`]+[ \t]*)*[ \t]*[.!?,;:]*[ \t]*$"
    ),
)

# A body completion marker is an assertion only when its whole line is the
# declaration (optionally bulleted/emphasised and with terminal punctuation).
_BARE_COMPLETE_MARKER = re.compile(
    r"(?m)^[ \t]*(?:[-*+][ \t]+)?[ \t]*(?:[*_]{1,3})?[ \t]*"
    r"(PACT_COMPLETE)[ \t]*[.!?,;:]*[ \t]*(?:[*_]{1,3})?[ \t]*[.!?,;:]*[ \t]*$"
)

# A declaration immediately preceded by a negation or an attribution is not a
# current affirmative conclusion.
_MARKER_PREVIOUS_CONFLICT = re.compile(
    r"(?i)\b(?:do\s+not|don'?t|not|previous|earlier|prior|former|last|old|"
    r"before|already|quoting|recorded|said|quoted)\b"
)

# A body declaration may be followed only by structural, non-prose content:
# whitespace, empty structured gap blocks, check-decision blocks, or Markdown
# horizontal rules. Anything else keeps the ordinary repair path.
_HORIZONTAL_RULE = re.compile(
    r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})[ \t]*$"
)
_CHECK_DECISION_BLOCK = re.compile(
    r"<pact-check-decisions\b[^>]*>\s*.*?\s*</pact-check-decisions\s*>",
    re.DOTALL | re.IGNORECASE,
)
_SELF_CLOSING_CHECK_DECISION_BLOCK = re.compile(
    r"<pact-check-decisions\b[^>]*/\s*>", re.IGNORECASE
)
_ALLOWED_SUFFIX_TAG = re.compile(
    r"</?pact-(?:gaps|check-decisions)\b", re.IGNORECASE
)
_UNRUN_VERIFICATION = re.compile(
    r"(?i)\b(?:not|never|hasn'?t|haven'?t|hadn'?t|didn'?t|doesn'?t|don'?t|"
    r"wasn'?t|weren'?t|isn'?t|aren'?t)\s+"
    r"(?:(?:yet|still|all|fully|actually|both|every)\s+)*"
    r"(?:been\s+)?"
    r"(?:(?:yet|still|all|fully|actually|both|every)\s+)*"
    r"(?:run|ran)\b"
)


def _trailing_object(
    text: str, *, plain_only: bool = True
) -> tuple[dict[str, Any], int, int] | None:
    """Return the last JSON object that reaches the end of ``text``.

    The object must be the final non-whitespace content. It is not enough that
    the decoder can read it: a quoted or fenced copy of a decision is evidence
    about the review, not the review's own conclusion. ``plain_only=False`` is
    used only to detect such a copy without treating it as the decision.
    """
    stripped = text.strip()
    decoder = json.JSONDecoder()
    for index in range(len(stripped) - 1, -1, -1):
        if stripped[index] != "{":
            continue
        try:
            value, end = decoder.raw_decode(stripped, index)
        except (json.JSONDecodeError, ValueError, RecursionError):
            continue
        if not isinstance(value, dict):
            continue
        if stripped[end:].strip():
            continue
        if plain_only and _position_is_quote_or_code(stripped, index, end):
            continue
        return value, index, end
    return None


def _line_is_indented_code(lines: list[str], index: int) -> bool:
    """Return whether one line belongs to a Markdown indented code block."""
    if index < 0 or index >= len(lines):
        return False
    line = lines[index]
    if not (line.startswith("    ") or line.startswith("\t")):
        return False
    # Walk to the first line of this indented run. A tab or four leading spaces
    # makes it code only when the run starts at the document start or after a
    # blank line; an indented continuation of a paragraph is ordinary prose.
    start = index
    while start > 0 and (
        lines[start - 1].startswith("    ") or lines[start - 1].startswith("\t")
    ):
        start -= 1
    if start == 0:
        return True
    return not lines[start - 1].strip()


def _position_is_quote_or_code(text: str, start: int, end: int) -> bool:
    """Return whether ``text[start:end]`` is quoted Markdown or code."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    start_line = text.count("\n", 0, start)
    end_line = text.count("\n", 0, end)
    all_lines = text.splitlines()

    # A fenced code block is opened by any line through the object's first line.
    fence: str | None = None
    for number, line in enumerate(all_lines):
        if number > start_line:
            break
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker[0]
                if number == start_line:
                    return True
            elif marker[0] == fence:
                fence = None
                if number == start_line:
                    return True
    if fence is not None:
        return True

    # Four spaces or one tab after a blank line opens an indented code block.
    if _line_is_indented_code(all_lines, start_line) or _line_is_indented_code(all_lines, end_line):
        return True

    # Every line occupied by the object must be ordinary body text. A leading
    # '>' means the decision is part of a block quotation.
    object_lines = all_lines[start_line : end_line + 1]
    if any(line.lstrip().startswith(">") for line in object_lines):
        return True

    # An unmatched backtick run before the object on its first line opens an
    # inline code span around the decision.
    first_line = text[line_start:line_end]
    relative_start = start - line_start
    runs = list(re.finditer(r"`+", first_line[:relative_start]))
    open_run: str | None = None
    for run in runs:
        if open_run is None:
            open_run = run.group()
        elif run.group() == open_run:
            open_run = None
    return open_run is not None


def _has_value(value: Any) -> bool:
    """Return whether a gap payload actually names a gap."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        # A dictionary key is itself a gap description, even when its value is
        # null (for example {"Login returns HTTP 500": null}).
        return any(
            (isinstance(key, str) and bool(key.strip())) or _has_value(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_has_value(item) for item in value)
    # A number or any other JSON scalar is non-empty content.
    return True


def _gap_block_has_content(payload: str) -> bool:
    """Parse one structured gap block; empty/null/blank means no gaps."""
    if not payload.strip():
        return False
    try:
        value = json.loads(payload)
    except (TypeError, ValueError, RecursionError):
        # An unparseable structured declaration cannot establish "no gaps";
        # keep the conservative repair path.
        return True
    return _has_value(value)


def _has_nonempty_structured_gaps(prose: str) -> bool:
    """Return whether any ``<pact-gaps>`` block names a real gap."""
    for match in _GAP_BLOCK.finditer(prose):
        if _gap_block_has_content(match.group(1)):
            return True
    # Remove complete and self-closing blocks, then treat an unmatched tag as
    # malformed rather than silently assuming the Reviewer declared no gaps.
    remaining = _GAP_BLOCK.sub(" ", prose)
    remaining = _SELF_CLOSING_GAP_BLOCK.sub(" ", remaining)
    return bool(re.search(r"</?pact-gaps\b", remaining, re.IGNORECASE))


def _body_marker_suffix_is_structural(suffix: str) -> bool:
    """Return whether only allowed structural content follows a body marker.

    Complete gap blocks must be empty/null. Check-decision blocks may carry a
    payload. After consuming complete blocks (including multiline blocks), every
    remaining non-empty line must be a Markdown horizontal rule; an unmatched
    tag or any prose is not a structural suffix.
    """
    for match in _GAP_BLOCK.finditer(suffix):
        if _gap_block_has_content(match.group(1)):
            return False
    remaining = _GAP_BLOCK.sub(" ", suffix)
    remaining = _SELF_CLOSING_GAP_BLOCK.sub(" ", remaining)
    remaining = _CHECK_DECISION_BLOCK.sub(" ", remaining)
    remaining = _SELF_CLOSING_CHECK_DECISION_BLOCK.sub(" ", remaining)
    if _ALLOWED_SUFFIX_TAG.search(remaining):
        return False
    return all(
        not line.strip() or _HORIZONTAL_RULE.fullmatch(line)
        for line in remaining.splitlines()
    )


def _has_plain_complete_marker(text: str) -> bool:
    """Return whether body text has a current affirmative completion declaration."""
    lines = text.splitlines()
    for match in _BARE_COMPLETE_MARKER.finditer(text):
        # A quotation, fence, inline code span or indented code block is not a
        # declaration made by this review.
        if _position_is_quote_or_code(text, match.start(1), match.end(1)):
            continue
        # Attribution and negation on the immediately preceding non-empty line
        # make the marker an earlier/quoted statement rather than this review's
        # conclusion.
        marker_line = text.count("\n", 0, match.start())
        previous = ""
        for index in range(marker_line - 1, -1, -1):
            if lines[index].strip():
                previous = lines[index]
                break
        if _MARKER_PREVIOUS_CONFLICT.search(previous):
            continue
        # Only structural content may follow the declaration. Any prose
        # follower, even affirmative prose, keeps the ordinary repair path.
        line_end = text.find("\n", match.end())
        subsequent = text[line_end + 1:] if line_end >= 0 else ""
        if _body_marker_suffix_is_structural(subsequent):
            return True
    return False


def _is_gap_field(name: str) -> bool:
    normalized = name.strip().lower().replace("-", "_")
    return (
        normalized
        in {
            "gap",
            "gaps",
            "gap_id",
            "gap_ids",
            "gap_signature",
            "blocking",
            "blockers",
            "blocking_gaps",
            "blocking_issues",
        }
        or normalized.endswith(
            ("_gap", "_gaps", "_gap_ids", "_gap_signature", "_blocking", "_blockers")
        )
    )


def _is_outcome_field(name: str) -> bool:
    return name.strip().lower().replace("-", "_") in _OUTCOME_FIELDS


def _has_negative_outcome(value: Any) -> bool:
    """Return whether an outcome value says failed/blocked/rejected/incomplete."""
    if isinstance(value, str):
        return bool(_NEGATIVE_OUTCOME.search(value))
    if isinstance(value, (list, tuple, set)):
        return any(_has_negative_outcome(item) for item in value)
    if isinstance(value, dict):
        return any(_has_negative_outcome(item) for item in value.values())
    return False


def _payload_has_conflict(payload: dict[str, Any]) -> bool:
    """Return whether any structured field in one JSON object blocks completion."""
    for name, value in payload.items():
        if _is_gap_field(name) and _has_value(value):
            return True
        if _is_outcome_field(name) and _has_negative_outcome(value):
            return True
    return False


def _iter_json_objects(text: str):
    """Yield top-level JSON objects with their source span."""
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            return
        try:
            value, end = decoder.raw_decode(text, start)
        except (json.JSONDecodeError, ValueError, RecursionError):
            index = start + 1
            continue
        if isinstance(value, dict):
            yield value, start, end
            index = end
        else:
            index = start + 1


def _has_nonterminal_decision_json(text: str) -> bool:
    """Return whether a decision-like JSON object is followed by body text."""
    for payload, start, end in _iter_json_objects(text):
        if not any(
            name == "verdict" or _is_gap_field(name) or _is_outcome_field(name)
            for name in payload
        ):
            continue
        if not text[end:].strip():
            continue
        if _position_is_quote_or_code(text, start, end):
            continue
        return True
    return False


def _has_conflicting_json(prose: str) -> bool:
    """Return whether an earlier JSON object names gaps or a negative outcome."""
    for payload, start, end in _iter_json_objects(prose):
        if _position_is_quote_or_code(prose, start, end):
            continue
        if _payload_has_conflict(payload):
            return True
    return False


def _normalized_line_label(line: str) -> str:
    """Normalize one line so markdown emphasis and punctuation do not hide it."""
    cleaned = re.sub(r"[*_~`]+", " ", line)
    cleaned = re.sub(r"[^\w\s]+", " ", cleaned)
    return " ".join(cleaned.upper().split())


def _has_standalone_negative_conclusion(text: str) -> bool:
    """Return whether a whole body line is only a failure/rejection conclusion."""
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        content = raw_line.rstrip("\r\n")
        start = offset
        end = start + len(content)
        offset += len(raw_line)
        if _normalized_line_label(content) not in _NEGATIVE_CONCLUSION_LABELS:
            continue
        if not _position_is_quote_or_code(text, start, end):
            return True
    return False


def _tested_input_list(match: re.Match[str]) -> str:
    if re.search(r"(?i)\b(?:unexpected(?:ly)?|regressions?|exceptions? remain|should succeed|withheld)\b", match[0]):
        return match[0]

    def keep_review_refusal(item: re.Match[str]) -> str:
        return item[0] if _REVIEW_OBJECT.search(item["inputs"]) else item[0].rsplit(None, 1)[0]

    return _INPUT_PAIR_REFUSAL.sub(keep_review_refusal, match[0])


def _has_conflicting_body(prose: str) -> bool:
    """Conservatively detect rejection or non-empty structured gaps."""
    if _has_nonempty_structured_gaps(prose) or _CONFLICT_MARKER.search(prose):
        return True
    if _NEGATED_COMPLETE.search(prose):
        return True
    if _UNRUN_VERIFICATION.search(prose):
        return True
    if _has_standalone_negative_conclusion(prose):
        return True
    if _has_conflicting_json(prose):
        return True
    checked = _NEGATED_CONFLICT.sub(" ", prose)
    checked = _GAP_NONE.sub(" ", checked)
    checked = _NO_UNMET.sub(" ", checked)
    checked = _NOTHING_WRONG.sub(" ", checked)
    checked = _NEGATED_DEFECT.sub(" ", checked)
    return any(pattern.search(checked) for pattern in _CONFLICT_PATTERNS)


def _valid_decision(payload: dict[str, Any]) -> bool:
    """Accept an unambiguous complete decision with common metadata.

    The verdict must be the string ``complete``. ``reason`` and
    ``plan_challenge`` retain their Core types, and common extra diagnostic
    fields are allowed. A non-empty gap field or a failed/blocked/rejected
    outcome value conflicts with completion; an empty/null one does not.
    """
    if _payload_has_conflict(payload):
        return False
    for name in payload:
        if _is_gap_field(name):
            continue
        if name not in _ALLOWED_DECISION_FIELDS:
            return False
    verdict = payload.get("verdict")
    if not isinstance(verdict, str) or verdict != _COMPLETE:
        # A non-string, list or object verdict is not a legal decision. Never
        # hash it, and never let it reach the transport shortcut.
        return False
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        return False
    challenge = payload.get("plan_challenge")
    if challenge is not None and not isinstance(challenge, dict):
        return False
    return True


def normalize_review_text(text: str) -> str:
    """Append ``PACT_COMPLETE`` for an unambiguous trailing complete decision.

    A text that Core already parses is returned unchanged. A trailing object
    that is quoted, fenced, inside inline code, has invalid fields/verdict, or
    conflicts with an explicit rejection, a non-empty structured gap block, or
    a failure marker is also left unchanged so the ordinary repair/protocol-
    error path still applies. Empty/whitespace/``null`` gap blocks do not
    conflict with completion.
    """
    if not text or not text.strip():
        return text
    if parse_review_decision(text, existed=True).verdict_status == "valid":
        return text
    trailing = _trailing_object(text)
    if trailing is None:
        # A decision object followed by prose is not terminal: the trailing
        # object must be the last non-whitespace content.
        if _has_nonterminal_decision_json(text):
            return text
        # A quoted/fenced/inline decision must not be promoted. But if the
        # review already asserts PACT_COMPLETE in ordinary body text, make that
        # existing assertion terminal so a valid repair is not rejected as a
        # newly introduced verdict.
        if _trailing_object(text, plain_only=False) is not None:
            return text
        if not _has_plain_complete_marker(text):
            return text
        if _has_conflicting_body(text.strip()):
            return text
        return _with_complete_marker(text)
    payload, start, _ = trailing
    if not _valid_decision(payload):
        return text
    body = text.strip()[:start]
    empty = _EMPTY_GAPS_BEFORE_COMPLETE.search(body.rstrip())
    if (
        empty
        and not _position_is_quote_or_code(body, empty.start(1), empty.end(1))
        and _has_plain_complete_marker(body)
        and not _UNRESOLVED_REVIEW.search(body)
    ):
        # Both the current review and its trailing decision declare completion.
        # Ignore only the two positive evidence forms when checking for conflicts.
        body = _EMPHASIZED_EMPTY_GAP.sub(" ", body)
        body = _TESTED_INPUT_LIST.sub(_tested_input_list, body)
    if _has_conflicting_body(body):
        return text
    return _with_complete_marker(text)


def _with_complete_marker(text: str) -> str:
    """Append the completion marker unless the review already ends terminally."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return text
    if lines[-1] == _COMPLETE_MARKER:
        return text
    if lines[-1] in {"PACT_COMPLETE", "PACT_FAILED", "PACT_BLOCKED"}:
        # The review already has a different terminal outcome.
        return text
    return text.rstrip() + "\n\n" + _COMPLETE_MARKER + "\n"


def acceptance_review_text(text: str) -> str:
    """Return the transport-normalized text for one acceptance Reviewer turn."""
    return normalize_review_text(text)


__all__ = ["acceptance_review_text", "normalize_review_text"]
