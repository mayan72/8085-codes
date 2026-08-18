"""Shared parsing helpers for AI forecast JSON and BASE_ANALYSIS headings.

These functions are Django-free so they can be unit-tested and dropped into
helpers/forecast_text_utils.py in the application repo.
"""

from __future__ import annotations

import json
import re


class ForecastTextError(ValueError):
    """Raised when model output or stored analysis cannot be parsed."""


_FENCE_OPEN_RE = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r"\s*```$")

# Matches "Short-term Outlook" / "Short Term Outlook" / markdown / HTML wrappers.
# The heading may be alone on a line or followed by the first bullet on the same line.
_OUTLOOK_HEADING_RE = {
    "short": re.compile(
        r"(?im)^(?P<prefix>[ \t]*(?:[#>*]+[ \t]*)?(?:<(?:h[1-6]|p|div|strong|b|span)[^>]*>)?[ \t]*(?:\*{1,3}|_{1,2})?[ \t]*)"
        r"Short[ \t]*[\s\-–—]+[ \t]*term[ \t]+Outlook"
        r"(?P<suffix>[ \t]*(?:\*{1,3}|_{1,2})?[ \t]*(?:</(?:h[1-6]|p|div|strong|b|span)>)?[ \t]*[:\-–—]?[ \t]*)"
        r"(?P<rest>.*)$"
    ),
    "long": re.compile(
        r"(?im)^(?P<prefix>[ \t]*(?:[#>*]+[ \t]*)?(?:<(?:h[1-6]|p|div|strong|b|span)[^>]*>)?[ \t]*(?:\*{1,3}|_{1,2})?[ \t]*)"
        r"Long[ \t]*[\s\-–—]+[ \t]*term[ \t]+Outlook"
        r"(?P<suffix>[ \t]*(?:\*{1,3}|_{1,2})?[ \t]*(?:</(?:h[1-6]|p|div|strong|b|span)>)?[ \t]*[:\-–—]?[ \t]*)"
        r"(?P<rest>.*)$"
    ),
}


def find_outlook_heading(text, which="short"):
    """Return (heading_start, content_start, rest_of_heading_line) or None."""
    pattern = _OUTLOOK_HEADING_RE[which]
    match = pattern.search(text or "")
    if not match:
        return None
    rest = match.group("rest") or ""
    content_start = match.end() - len(rest)
    return match.start(), content_start, rest


def extract_first_short_term_bullet(analysis):
    """Return (original_bullet, start_index, end_index) from the narrative."""
    text = analysis or ""
    heading = find_outlook_heading(text, "short")
    if not heading:
        raise ForecastTextError("BASE_ANALYSIS is missing 'Short-term Outlook'")

    _heading_start, section_start, rest_on_heading_line = heading

    long_heading = find_outlook_heading(text[section_start:], "long")
    if long_heading:
        section_end = section_start + long_heading[0]
    else:
        section_end = len(text)

    section = text[section_start:section_end]
    first_nonspace = re.search(r"\S", section)
    if not first_nonspace:
        raise ForecastTextError("Short-term Outlook section is empty")

    bullet_start_rel = first_nonspace.start()
    remainder = section[bullet_start_rel:]
    lines = remainder.splitlines(keepends=True)
    if not lines:
        raise ForecastTextError("Unable to parse first short-term bullet")

    marker_match = re.match(r"\s*(?:[-*•]|\d+[.)])\s+", lines[0])
    end_rel = len(remainder)

    if marker_match:
        marker_pattern = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
        consumed = len(lines[0])
        for line in lines[1:]:
            if marker_pattern.match(line):
                end_rel = consumed
                break
            consumed += len(line)
    else:
        paragraph_break = re.search(r"\n\s*\n", remainder)
        if paragraph_break:
            end_rel = paragraph_break.start()

    start = section_start + bullet_start_rel
    end = start + end_rel
    while end > start and text[end - 1].isspace():
        end -= 1
    original = text[start:end].rstrip()
    if not original:
        raise ForecastTextError("Parsed first short-term bullet is empty")
    return original, start, end


def _strip_fences(raw_text):
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = _FENCE_OPEN_RE.sub("", text, count=1)
        text = _FENCE_CLOSE_RE.sub("", text)
    return text.strip()


def _extract_json_object_text(text):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ForecastTextError("Forecast response is not a JSON object")
    return text[start : end + 1]


def _repair_json_text(text):
    repaired = text.replace("\ufeff", "")
    repaired = (
        repaired.replace("“", '"')
        .replace("”", '"')
        .replace("„", '"')
        .replace("‟", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
    repaired = re.sub(r"}\s*{", "},{", repaired)
    repaired = re.sub(r"]\s*\[", "],[", repaired)
    return repaired


def parse_json_object(raw_text):
    """Parse a single JSON object from model output.

    Tolerates markdown fences, surrounding prose, trailing commas, smart quotes,
    and unescaped control characters inside strings.
    """
    text = _extract_json_object_text(_strip_fences(raw_text))
    candidates = [text, _repair_json_text(text)]
    last_exc = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate, strict=False)
        except json.JSONDecodeError as exc:
            last_exc = exc
            continue
        if not isinstance(parsed, dict):
            raise ForecastTextError("Forecast response must be a JSON object")
        return parsed
    raise ForecastTextError(f"Invalid JSON: {last_exc}") from last_exc
