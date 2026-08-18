"""Parsing helpers for AI forecast JSON and BASE_ANALYSIS first-bullet extraction.

Django-free so these can be unit-tested and dropped into the application repo.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


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

# Production summaries use Ω as 1st-level, π as 2nd-level, Σ as 3rd-level.
_FIRST_LEVEL_BULLET_RE = re.compile(r"^[ \t]*(?:Ω|[-*•]|\d+[.)])[ \t]+")
_NESTED_BULLET_RE = re.compile(r"^[ \t]*(?:π|Σ)[ \t]+")


def find_outlook_heading(text, which="short"):
    """Return (heading_start, content_start, rest_of_heading_line) or None."""
    pattern = _OUTLOOK_HEADING_RE[which]
    match = pattern.search(text or "")
    if not match:
        return None
    rest = match.group("rest") or ""
    content_start = match.end() - len(rest)
    return match.start(), content_start, rest


def _trim_bullet_span(text, start, end):
    while end > start and text[end - 1].isspace():
        end -= 1
    original = text[start:end].rstrip()
    return original, start, end


def _end_of_first_bullet(remainder):
    """Return end offset of the first bullet inside remainder (relative)."""
    lines = remainder.splitlines(keepends=True)
    if not lines:
        return 0

    first_line = lines[0]
    first_level = _FIRST_LEVEL_BULLET_RE.match(first_line)
    end_rel = len(remainder)

    if first_level:
        consumed = len(first_line)
        for line in lines[1:]:
            if _FIRST_LEVEL_BULLET_RE.match(line) and not _NESTED_BULLET_RE.match(line):
                return consumed
            consumed += len(line)
        return end_rel

    paragraph_break = re.search(r"\n\s*\n", remainder)
    if paragraph_break:
        return paragraph_break.start()
    return end_rel


def extract_first_short_term_bullet(analysis):
    """Return (original_bullet, start_index, end_index) from the narrative.

    Prefers the first first-level bullet under Short-term Outlook. Nested
    π / Σ lines stay attached to that Ω bullet until the next Ω (or markdown)
    first-level marker.
    """
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
    if not remainder:
        raise ForecastTextError("Unable to parse first short-term bullet")

    end_rel = _end_of_first_bullet(remainder)
    start = section_start + bullet_start_rel
    end = start + end_rel
    original, start, end = _trim_bullet_span(text, start, end)
    if not original:
        raise ForecastTextError("Parsed first short-term bullet is empty")
    return original, start, end


def extract_first_omega_bullet(analysis):
    """Fallback: first Ω bullet anywhere in the analysis text."""
    text = analysis or ""
    match = re.search(r"(?m)^[ \t]*Ω[ \t]+", text)
    if not match:
        raise ForecastTextError("No Ω first-level bullet found in analysis")
    start = match.start()
    remainder = text[start:]
    end_rel = _end_of_first_bullet(remainder)
    original, start, end = _trim_bullet_span(text, start, start + end_rel)
    if not original:
        raise ForecastTextError("Parsed Ω bullet is empty")
    return original, start, end


def get_first_short_term_bullet_for_prompt(analysis, *, log=None, cp_id=None):
    """Resolve the first short-term bullet for <<ORIGINAL_FIRST_SHORT_TERM_BULLET>>.

    Tries Short-term Outlook first, then a global Ω bullet. Never raises;
    returns an empty string when nothing can be extracted.
    """
    log = log or logger
    text = analysis or ""
    cp_label = cp_id if cp_id is not None else "unknown"

    if not text.strip():
        log.warning(
            "[FIRST BULLET] cp_id=%s | source=empty | chars=0 | bullet=''",
            cp_label,
        )
        return ""

    try:
        bullet, start, end = extract_first_short_term_bullet(text)
        log.info(
            "[FIRST BULLET] cp_id=%s | source=short_term_outlook | start=%s | end=%s | chars=%s | words=%s",
            cp_label,
            start,
            end,
            len(bullet),
            len(bullet.split()),
        )
        log.info(
            "[FIRST BULLET TEXT] cp_id=%s | %s",
            cp_label,
            bullet[:1000],
        )
        return bullet
    except ForecastTextError as outlook_error:
        log.warning(
            "[FIRST BULLET] cp_id=%s | short_term_outlook failed | %s",
            cp_label,
            outlook_error,
        )

    try:
        bullet, start, end = extract_first_omega_bullet(text)
        log.info(
            "[FIRST BULLET] cp_id=%s | source=omega_fallback | start=%s | end=%s | chars=%s | words=%s",
            cp_label,
            start,
            end,
            len(bullet),
            len(bullet.split()),
        )
        log.info(
            "[FIRST BULLET TEXT] cp_id=%s | %s",
            cp_label,
            bullet[:1000],
        )
        return bullet
    except ForecastTextError as omega_error:
        log.warning(
            "[FIRST BULLET] cp_id=%s | omega_fallback failed | %s",
            cp_label,
            omega_error,
        )

    log.error(
        "[FIRST BULLET] cp_id=%s | unable to extract first bullet; prompt placeholder will be empty",
        cp_label,
    )
    return ""


SUMMARY_PROMPT_PLACEHOLDERS = (
    "<<NEWS_ARTICLES>>",
    "<<BASE_ANALYSIS>>",
    "<<COMMODITY_NAME>>",
    "<<REGION>>",
    "<<LAST_ACTUAL>>",
    "<<PREVIOUS_FORECAST>>",
    "<<FORECAST_PRICE>>",
    "<<ORIGINAL_FIRST_SHORT_TERM_BULLET>>",
)


def build_ai_summary_prompt(
    template,
    *,
    news_articles,
    base_articles,
    commodity_name,
    region,
    last_actual,
    previous_forecast,
    revised_forecast,
    first_short_term_bullet,
    log=None,
    cp_id=None,
):
    """Fill AI_SUMMARY_PROMPT placeholders, including the extracted first bullet.

    previous_forecast is Base_Forecast (the value being revised from).
    revised_forecast is New_Forecast (the value being revised to / FORECAST_PRICE).
    """
    log = log or logger
    cp_label = cp_id if cp_id is not None else "unknown"
    replacements = {
        "<<NEWS_ARTICLES>>": news_articles or "",
        "<<BASE_ANALYSIS>>": base_articles or "",
        "<<COMMODITY_NAME>>": commodity_name or "",
        "<<REGION>>": region or "",
        "<<LAST_ACTUAL>>": "" if last_actual is None else str(last_actual),
        "<<PREVIOUS_FORECAST>>": "" if previous_forecast is None else str(previous_forecast),
        "<<FORECAST_PRICE>>": "" if revised_forecast is None else str(revised_forecast),
        "<<ORIGINAL_FIRST_SHORT_TERM_BULLET>>": first_short_term_bullet or "",
    }

    prompt = template or ""
    missing = [key for key in SUMMARY_PROMPT_PLACEHOLDERS if key not in prompt]
    if missing:
        log.warning(
            "[SUMMARY PROMPT] cp_id=%s | template missing placeholders=%s",
            cp_label,
            ",".join(missing),
        )

    for key, value in replacements.items():
        prompt = prompt.replace(key, value)

    leftover = [key for key in SUMMARY_PROMPT_PLACEHOLDERS if key in prompt]
    if leftover:
        log.error(
            "[SUMMARY PROMPT] cp_id=%s | leftover placeholders after fill=%s",
            cp_label,
            ",".join(leftover),
        )
    else:
        log.info(
            "[SUMMARY PROMPT] cp_id=%s | placeholders filled | first_bullet_chars=%s | prompt_chars=%s",
            cp_label,
            len(replacements["<<ORIGINAL_FIRST_SHORT_TERM_BULLET>>"]),
            len(prompt),
        )
    return prompt


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
