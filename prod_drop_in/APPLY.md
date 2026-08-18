# Apply first-bullet extraction in the Django AI-forecast modules

The Django job is not in this 8085 repository. Copy these files into the app:

- `helpers/forecast_text_utils.py`
- `scripts/forecast_prompt.py` (`AI_SUMMARY_PROMPT` now includes `<<ORIGINAL_FIRST_SHORT_TERM_BULLET>>`)
- `scripts/main_fortecast_script.py` (extraction + prompt fill + loggers)

Keep `helpers/ai_forecast_helper.py` as-is. No OpenAI client changes are required for this step.

## What was added

`ai_summary_prompt_inputs` reads `summary` from the base-forecast row, then falls back to the AI-forecast row. It extracts the first Short-term Outlook bullet (Ω plus nested π / Σ lines) and returns it as `first_short_term_bullet`.

`generate_ai_sense` fills:

| Placeholder | Value |
| --- | --- |
| `<<ORIGINAL_FIRST_SHORT_TERM_BULLET>>` | extracted first bullet |
| `<<PREVIOUS_FORECAST>>` | `Base_Forecast` |
| `<<FORECAST_PRICE>>` | `New_Forecast` |

The old mapping swapped those two forecast placeholders. That is corrected here so the summary prompt's "revised from … to …" sentence matches the job.

## Loggers to watch

- `[BASE ANALYSIS SOURCE]` — whether summary came from base or AI forecast
- `[FIRST BULLET]` — source (`short_term_outlook`, `omega_fallback`, or `empty`), offsets, size
- `[FIRST BULLET TEXT]` — first 1000 characters of the extracted bullet
- `[SUMMARY PLACEHOLDERS]` — commodity, last actual, previous vs revised forecast
- `[SUMMARY PROMPT]` — leftover/missing placeholders after fill

## If Short-term Outlook is missing

Extraction does not fail the CP. The placeholder is filled with `""` and the prompt uses the missing-first-bullet rule. A warning is logged.

## Tests in this repo

```bash
python3 -m unittest tests.test_forecast_text_utils -v
```
