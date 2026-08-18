"""AI forecast and summary prompts.

Copy this file over scripts/forecast_prompt.py in the Django app.
AI_FORECAST_PROMPT is unchanged from the production job. AI_SUMMARY_PROMPT
now receives <<ORIGINAL_FIRST_SHORT_TERM_BULLET>> extracted from summary /
base analysis before the model call.
"""

# Keep the production forecast prompt in the Django app when merging this file.
# Only AI_SUMMARY_PROMPT / FIRST_BULLET_PROMPT_BLOCK need to be replaced.
AI_FORECAST_PROMPT = """
Keep the existing production AI_FORECAST_PROMPT. Merge AI_SUMMARY_PROMPT from
this file instead of replacing the forecast prompt.

USER_INPUTS:
<<USER_INPUTS_JSON>>
"""

ORIGINAL_FIRST_SHORT_TERM_BULLET_PLACEHOLDER = "<<ORIGINAL_FIRST_SHORT_TERM_BULLET>>"

FIRST_BULLET_PROMPT_BLOCK = """
# Original first short-term bullet (extracted from BASE_ANALYSIS / summary)
The current first bullet under Short-term Outlook is provided below so the
update target is unambiguous. Nested 2nd/3rd-level lines (π / Σ) that belong
to that first bullet are included.

ORIGINAL_FIRST_SHORT_TERM_BULLET:
<<ORIGINAL_FIRST_SHORT_TERM_BULLET>>

- Treat ORIGINAL_FIRST_SHORT_TERM_BULLET as the current first bullet.
- Rewrite ONLY that bullet per the Forecast update rule and Scope rules.
- Preserve its months, nested π / Σ lines, and bullet symbols unless a nested
  line is part of the allowed first-bullet rewrite.
- If ORIGINAL_FIRST_SHORT_TERM_BULLET is empty, follow the missing-first-bullet
  rule: add exactly one bullet under Short-term Outlook.
"""

AI_SUMMARY_PROMPT = """
# Role
You are updating an existing commodity outlook analysis using variable data and highly relevant recent market news.

# Core objective
Regenerate the analysis for <<COMMODITY_NAME>> in <<REGION>> using:
1) <<BASE_ANALYSIS>>
2) <<NEWS_ARTICLES>>
3) additional news gathered from the secondary domain

Preserve the original structure, hierarchy, and direction logic unless a change is explicitly allowed below.

# Original first short-term bullet (extracted from BASE_ANALYSIS / summary)
The current first bullet under Short-term Outlook is provided below so the
update target is unambiguous. Nested 2nd/3rd-level lines (π / Σ) that belong
to that first bullet are included.

ORIGINAL_FIRST_SHORT_TERM_BULLET:
<<ORIGINAL_FIRST_SHORT_TERM_BULLET>>

- Treat ORIGINAL_FIRST_SHORT_TERM_BULLET as the current first bullet.
- Rewrite ONLY that bullet per the Forecast update rule and Scope rules.
- Preserve its months, nested π / Σ lines, and bullet symbols unless a nested
  line is part of the allowed first-bullet rewrite.
- If ORIGINAL_FIRST_SHORT_TERM_BULLET is empty, follow the missing-first-bullet
  rule: add exactly one bullet under Short-term Outlook.

# Completion contract
- Return ONLY the regenerated analysis.
- Do not add any preamble, explanation, methodology, notes, or labels.
- At the top, print exactly:
  Commodity: <<COMMODITY_NAME>>
  Region: <<REGION>>
- Keep exactly these headings:
  Short-term Outlook
  Long-term Outlook
- Preserve all unchanged bullets verbatim.
- Change only what is explicitly allowed.
- Ensure the final output reads as one integrated analysis.

# Output cleanliness rule
- Do NOT include any citations, source attributions, references, referers, footnotes, endnotes, hyperlinks, URLs, domains, publisher names, article names, or source labels in the final output.
- Do NOT mention where the news came from.
- Do NOT use bracketed or parenthetical source markers of any kind.
- Convert all source-backed information into plain analytical prose only.
- The final response must contain analysis only, with no source trail or attribution language.

# Search rules
- In addition to <<NEWS_ARTICLES>>, conduct a deep scan on the secondary domain.
- Scan at least 2 credible articles from the past 24 hours.
- Search relevant countries within <<REGION>>.
- Stop searching once you have enough highly relevant evidence to support the allowed update; do not gather extra articles unless they materially improve accuracy.
- Use only highly relevant news.
- Integrate news directly into the analysis; do not present news separately.

# Scope rules
- Update ONLY the first bullet under "Short-term Outlook" (the text in ORIGINAL_FIRST_SHORT_TERM_BULLET).
- All other bullets/statements must remain exactly as they are.
- Do not rewrite, reorder, reframe, paraphrase, or split any other bullets.
- Preserve the original rise/fall direction of every other bullet exactly as in <<BASE_ANALYSIS>>.
- If the first short-term bullet is missing, add exactly one bullet under "Short-term Outlook".
- Do not add any other new bullets.

# Month lock rule
- If the first short-term bullet mentions specific months, keep the exact same months.
- Do not replace specific months with relative references such as "next month".

# Edge-case rule
If the first short-term bullet covers two consecutive months and only one month’s direction changes:
- Keep it as ONE bullet.
- Do not split it into multiple bullets.
- State the month-wise directions in the same sentence using wording such as "; however," or "while".
- Update the reasoning only for the month whose direction changed.
- Keep the other month’s clause aligned with its original direction and original driver.

Example pattern:
"Prices are expected to rise in Sep 2025 ...; however, prices are expected to fall in Oct 2025 ..."

# Forecast update rule
The last actual value of <<COMMODITY_NAME>> was <<LAST_ACTUAL>>.
The forecast for the next month has been revised from <<PREVIOUS_FORECAST>> to <<FORECAST_PRICE>>, implying an M-o-M revision.

Use that revision to rephrase ONLY the first short-term bullet:
- If the revised direction is rise, give concrete reasons supporting the rise.
- If the revised direction is fall, give concrete reasons supporting the fall.
- Use variable data and/or highly relevant news as support.
- Use at most ONE additional clause from news or another driver in the updated first bullet, and only if it is highly relevant.

# Direction consistency rule
- Do not change the direction of any other month or bullet.
- If a month rises in the base narrative, it must continue to rise unless it is the allowed update month portion within the first short-term bullet.
- If a month falls in the base narrative, it must continue to fall unless it is the allowed update month portion within the first short-term bullet.

# Wording rules
Do NOT:
- mention numeric revision values in the rewritten bullet
- mention last actual or forecast numbers in the rewritten bullet
- say "Prices are now expected to ..."
- say "Prices have been revised down/up ..."
- say "to X from Y"
- discuss the forecast revision explicitly inside the narrative sentence

Do:
- state only the resulting direction: rise / fall / stable
- explain that direction using concrete variable data or highly relevant news
- keep tense specific and logically aligned with the price direction
- frame future or predicted developments as likelihood/scenario-based where needed
- mention supportive and limiting factors explicitly

If a limiting factor is needed, use wording such as:
- "with gains capped by ..."
- "partly offset by ..."

Do NOT use vague or generic contradiction phrases such as:
- "even as fundamentals remain relatively soft"
- "amid mixed drivers"
- "with limited support from tracked drivers"
- "on broadly offsetting near-term drivers"

Do NOT use labels such as:
- "Market context:"
- "News:"
- "Macro context:"

Do not force news insights to the top; place them only where they fit best in the existing analysis flow.

# Addition cap
- Add at most ONE additional clause into the updated first short-term bullet, only if it is highly relevant.
- Do not add new independent bullets unless the first short-term bullet is missing.
- Any added news or driver must not contradict the stated direction.
- If it is offsetting, phrase it explicitly as capped/partly offset.

# Style rules
- Do not generate filler.
- Do not generate generic statements.
- Do not leave any price movement unexplained.
- Every stated direction must be supported by variable data or relevant news.
- Keep the analysis integrated and natural.

# Formatting rules
- 1st-level bullet symbol: Ω
- 2nd-level bullet symbol: π
- 3rd-level bullet symbol: Σ

# Bold rule
- Highlight changes to the base text in Bold.
- Bold ONLY the changed words/phrases.
- Do NOT bold the entire bullet.

# Final verification
Before returning the answer, verify all of the following:
- only the allowed first short-term bullet was updated
- ORIGINAL_FIRST_SHORT_TERM_BULLET identifies that allowed bullet
- all unchanged bullets remain unchanged
- no contradictions were introduced
- exact months were preserved where required
- no forbidden phrasing appears
- no citations, sources, referers, URLs, domains, links, or attribution text appear anywhere in the final answer
- headings and bullet symbols are correct
- sentence structure is complete and logical
- the final answer contains only the regenerated analysis
"""
