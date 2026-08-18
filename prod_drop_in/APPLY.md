# Apply these changes in the Django AI-forecast modules

Copy `helpers/forecast_text_utils.py` into the app as `helpers/forecast_text_utils.py`.
Then apply the replacements below in the three existing modules.

These two production failures are covered:

1. `BASE_ANALYSIS is missing 'Short-term Outlook'`
2. `Invalid JSON: Expecting ',' delimiter ...`

## 1. Forecast job module (the file that defines `_parse_json_object` / `ai_forecast_output`)

### Imports

```python
from helpers.forecast_text_utils import (
    ForecastTextError,
    extract_first_short_term_bullet,
    parse_json_object,
)
```

### Replace `_parse_json_object`

```python
def _parse_json_object(raw_text):
    try:
        return parse_json_object(raw_text)
    except ForecastTextError as exc:
        raise ForecastOutputError(str(exc)) from exc
```

### Replace `_extract_first_short_term_bullet`

```python
def _extract_first_short_term_bullet(analysis):
    try:
        return extract_first_short_term_bullet(analysis)
    except ForecastTextError as exc:
        raise ForecastOutputError(str(exc)) from exc
```

### Forecast OpenAI call — request JSON object output

Pass `text_format="json_object"` into `create_openai_response` for the forecast call only (keep summary as plain text).

### `generate_ai_sense` placeholders

The summary prompt uses `<<PREVIOUS_FORECAST>>` and `<<REVISED_FORECAST>>`. Wire them as:

```python
.replace("<<LAST_ACTUAL>>", str(last_actual))
.replace("<<PREVIOUS_FORECAST>>", str(base_forecast))
.replace("<<REVISED_FORECAST>>", str(new_forecast))
.replace("<<FORECAST_PRICE>>", str(new_forecast))
```

Do not map `PREVIOUS_FORECAST` to `new_forecast`.

### Summary scoring when the original analysis cannot be parsed

If `generate_ai_sense` already preserved the original analysis, do not mark the CP `FAILED` only because scoring cannot find the heading. In the `except ForecastOutputError as summary_eval_exc` block:

```python
except ForecastOutputError as summary_eval_exc:
    if (summary or "").strip() == (original_analysis or "").strip():
        summary_status = "PRESERVED"
        warnings.append(str(summary_eval_exc))
        correctness_C = correctness_C or "N/A"
        faithfulness_F = faithfulness_F or "N/A"
        faithfulness_G = faithfulness_G or "N/A"
    else:
        summary_status = "FAILED"
        errors.append(str(summary_eval_exc))
        correctness_C = correctness_C or "N/A"
        faithfulness_F = faithfulness_F or "FAIL"
        faithfulness_G = faithfulness_G or "N/A"
```

## 2. `helpers/ai_forecast_helper_copy.py` — `create_openai_response`

Add `text_format="text"` and use it:

```python
def create_openai_response(
    client,
    final_prompt,
    MODEL_NAME,
    REASONING_LEVEL,
    *,
    max_output_tokens=8000,
    verbosity="medium",
    enable_web_search=True,
    search_context_size="medium",
    call_name="ai_forecast",
    text_format="text",
):
```

```python
        format_type = text_format or "text"
        request_kwargs = {
            "model": MODEL_NAME,
            "input": final_prompt,
            "text": {
                "format": {"type": format_type},
                "verbosity": verbosity,
            },
            "reasoning": {"effort": REASONING_LEVEL},
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
```

Forecast call:

```python
                response = create_openai_response(
                    client=client,
                    final_prompt=final_prompt,
                    MODEL_NAME=MODEL_NAME,
                    REASONING_LEVEL=FORECAST_REASONING_LEVEL,
                    max_output_tokens=FORECAST_MAX_OUTPUT_TOKENS,
                    verbosity="medium",
                    enable_web_search=True,
                    search_context_size="medium",
                    call_name=f"forecast:{cp_id}",
                    text_format="json_object",
                )
```

## 3. `helpers/ai_forecast_run_tracker.py`

Replace the tracker copy of `_extract_first_short_term_bullet` with the same wrapper so faithfulness scoring uses the relaxed heading matcher:

```python
from helpers.forecast_text_utils import (
    ForecastTextError,
    extract_first_short_term_bullet,
)

def _extract_first_short_term_bullet(analysis):
    try:
        return extract_first_short_term_bullet(analysis)
    except ForecastTextError as exc:
        raise TrackingExtractError(str(exc)) from exc
```

## 4. `scripts/forecast_prompt_copy.py`

In `AI_FORECAST_PROMPT`, keep `Return JSON only.` and add:

```
Escape every JSON string. Do not put raw newlines or unescaped double quotes inside string values.
```
