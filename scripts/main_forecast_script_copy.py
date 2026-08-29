import json
import os
import re
from datetime import datetime

import requests
from dateutil import parser as date_parser
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.commodity_price.models import LoadCPAIForecast
from apps.load_layer.models import LoadCP
from helpers.ai_forecast_helper_copy import (
    convert_forecast_month_to_date,
    create_openai_response,
    get_openai_client,
)
from helpers.ai_forecast_calc import assemble_forecast_output
from helpers.config import (
    base_forecast_logger as logger,
    category_new_api_end_point,
    category_new_api_start_point_PROD,
)
from scripts.forecast_prompt_copy import AI_FORECAST_PROMPT, AI_SUMMARY_PROMPT

from helpers.ai_forecast_run_tracker import (
    ForecastRunTracker,
    detect_bullet_direction,
    required_price_direction,
    score_correctness_c,
    score_faithfulness_f,
    score_faithfulness_g,
    score_faithfulness_h,
    usage_from_response,
)


MODEL_NAME = getattr(settings, "AI_FORECAST_MODEL", "gpt-5.6-luna")
FORECAST_REASONING_LEVEL = getattr(settings, "AI_FORECAST_REASONING_LEVEL", "medium")
SUMMARY_REASONING_LEVEL = getattr(settings, "AI_SUMMARY_REASONING_LEVEL", "medium")
FORECAST_MAX_OUTPUT_TOKENS = int(
    getattr(settings, "AI_FORECAST_MAX_OUTPUT_TOKENS", 12000)
)
SUMMARY_MAX_OUTPUT_TOKENS = int(
    getattr(settings, "AI_SUMMARY_MAX_OUTPUT_TOKENS", 2500)
)
MAX_NEWS_ARTICLES = int(getattr(settings, "AI_FORECAST_MAX_NEWS_ARTICLES", 20))
NEWS_LOOKBACK_HOURS = int(getattr(settings, "AI_FORECAST_NEWS_LOOKBACK_HOURS", 720))
NEWS_API_LIMIT = int(getattr(settings, "AI_FORECAST_NEWS_API_LIMIT", 200))
MAX_SUMMARY_BULLET_WORDS = int(
    getattr(settings, "AI_SUMMARY_MAX_BULLET_WORDS", 220)
)


class ForecastOutputError(ValueError):
    """Raised when model output violates the forecast contract."""


def _setting_or_env(name, default=None):
    value = getattr(settings, name, None)
    if value not in (None, ""):
        return value
    return os.getenv(name, default)


def _resolve_region(row):
    """Use known model attributes when available without requiring a schema change."""
    for field_name in ("region", "geography", "geography_name", "region_name"):
        value = getattr(row, field_name, None)
        if value:
            return str(value).strip()
    return "Global"


def _resolve_benchmark_hint(row):
    for field_name in ("benchmark_hint", "region_or_benchmark_hint", "benchmark"):
        value = getattr(row, field_name, None)
        if value:
            return str(value).strip()
    return None


def _baseline_value(row, using_ai_fallback):
    """
    Preserve the existing behavior by default: da_forecast remains the anchor even
    when AI rows are used as the source. Set AI_FORECAST_CHAIN_FROM_LATEST_AI=True
    only if the business rule is to compound each refresh from forecast_price.
    """
    chain_from_latest_ai = bool(
        getattr(settings, "AI_FORECAST_CHAIN_FROM_LATEST_AI", False)
    )
    if (
        using_ai_fallback
        and chain_from_latest_ai
        and getattr(row, "forecast_price", None) not in (None, "")
    ):
        return float(row.forecast_price)
    return float(row.da_forecast)


def get_data_from_base_forecast(cp_id, optional_news_dump="", region=None):
    """Build deterministic input for the forecast-adjustment prompt."""
    qs = LoadCPAIForecast.objects.filter(
        cp_id=cp_id,
        active=True,
        base_forecast_flag=True,
    ).order_by("timeframe")

    using_ai_fallback = False
    if not qs.exists():
        using_ai_fallback = True
        qs = LoadCPAIForecast.objects.filter(
            cp_id=cp_id,
            active=True,
            ai_forecast_flag=True,
        ).order_by("timeframe")

    if not qs.exists():
        return {}

    rows = list(qs)
    first_row = rows[0]
    baseline_rows = []

    for row in rows:
        if not row.timeframe:
            logger.warning("Skipping forecast row without timeframe | cp_id=%s", cp_id)
            continue
        try:
            base_value = _baseline_value(row, using_ai_fallback)
        except (TypeError, ValueError):
            logger.warning(
                "Skipping non-numeric forecast row | cp_id=%s timeframe=%s",
                cp_id,
                row.timeframe,
            )
            continue

        baseline_rows.append(
            {
                "Forecast_dates": row.timeframe.strftime("%b-%y"),
                "Base_Forecast": base_value,
            }
        )

    if not baseline_rows:
        return {}

    run_date = timezone.now().date()
    run_region = region or _resolve_region(first_row)
    benchmark_hint = _resolve_benchmark_hint(first_row)

    run_context = {
        "run_date_local": run_date.strftime("%Y-%m-%d"),
        "commodity_id": str(first_row.cp_id),
        "commodity_name": first_row.cp_name,
        "unit_name": first_row.unit_name,
        "region": run_region,
        "month_label": run_date.strftime("%b-%Y"),
        "baseline_source": "latest_ai_forecast" if using_ai_fallback else "base_forecast",
    }
    if benchmark_hint:
        run_context["region_or_benchmark_hint"] = benchmark_hint

    return {
        "Run_Context": run_context,
        "Baseline_Forecast_Rows": baseline_rows,
        "Optional_News_Dump": optional_news_dump or "",
    }


def _parse_json_object(raw_text):
    """Parse a single JSON object, tolerating accidental fenced output for resilience."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ForecastOutputError(f"Invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ForecastOutputError("Forecast response must be a JSON object")
    return parsed


def _require_number(value, field_name, allow_none=False):
    if value is None and allow_none:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ForecastOutputError(f"{field_name} must be numeric")


def validate_forecast_output(payload, baseline_rows, run_context):
    """Validate the model contract and the application-critical invariants."""
    required_top_level = {
        "cp_id",
        "cp_name",
        "unit_name",
        "run_date_local",
        "month_label",
        "benchmark_selected",
        "week_of_month",
        "weights",
        "price_data",
        "news_dump",
        "news_inputs_used",
        "combined_signal",
        "tapering",
        "forecast",
        "New_Summary",
        "Calculation_Notes",
    }
    missing = required_top_level - set(payload)
    if missing:
        raise ForecastOutputError(f"Missing top-level fields: {sorted(missing)}")

    if str(payload["cp_id"]) != str(run_context["commodity_id"]):
        raise ForecastOutputError("cp_id does not match input commodity_id")
    if str(payload["run_date_local"]) != str(run_context["run_date_local"]):
        raise ForecastOutputError("run_date_local does not match input")
    if str(payload["month_label"]) != str(run_context["month_label"]):
        raise ForecastOutputError("month_label does not match input")

    week = payload.get("week_of_month")
    if not isinstance(week, int) or isinstance(week, bool) or not 1 <= week <= 5:
        raise ForecastOutputError("week_of_month must be an integer from 1 to 5")

    weights = payload.get("weights")
    if not isinstance(weights, dict):
        raise ForecastOutputError("weights must be an object")
    news_weight = weights.get("news")
    avg_weight = weights.get("avg_to_date")
    _require_number(news_weight, "weights.news")
    _require_number(avg_weight, "weights.avg_to_date")
    if not 0 <= news_weight <= 1 or not 0 <= avg_weight <= 1:
        raise ForecastOutputError("weights must each be between 0 and 1")
    weight_sum = news_weight + avg_weight
    if not (abs(weight_sum - 1.0) <= 0.001 or abs(weight_sum) <= 0.001):
        raise ForecastOutputError("effective weights must sum to 1, or 0 if both signals are unavailable")

    news_dump = payload.get("news_dump")
    if not isinstance(news_dump, list):
        raise ForecastOutputError("news_dump must be an array")
    if len(news_dump) > MAX_NEWS_ARTICLES:
        raise ForecastOutputError(
            f"news_dump exceeds configured maximum of {MAX_NEWS_ARTICLES}"
        )

    forecast_rows = payload.get("forecast")
    if not isinstance(forecast_rows, list):
        raise ForecastOutputError("forecast must be an array")
    if len(forecast_rows) != len(baseline_rows):
        raise ForecastOutputError(
            f"forecast length {len(forecast_rows)} != input length {len(baseline_rows)}"
        )

    for index, (row, input_row) in enumerate(zip(forecast_rows, baseline_rows)):
        if not isinstance(row, dict):
            raise ForecastOutputError(f"forecast[{index}] must be an object")
        for key in (
            "Forecast_dates",
            "Base_Forecast",
            "New_Forecast",
            "horizon_index",
            "adj_pct_total",
            "taper_multiplier",
        ):
            if key not in row:
                raise ForecastOutputError(f"forecast[{index}] missing {key}")

        if str(row["Forecast_dates"]) != str(input_row["Forecast_dates"]):
            raise ForecastOutputError(
                f"forecast[{index}].Forecast_dates changed from input"
            )

        _require_number(row["Base_Forecast"], f"forecast[{index}].Base_Forecast")
        _require_number(row["New_Forecast"], f"forecast[{index}].New_Forecast")
        _require_number(row["adj_pct_total"], f"forecast[{index}].adj_pct_total")
        _require_number(row["taper_multiplier"], f"forecast[{index}].taper_multiplier")

        input_base = float(input_row["Base_Forecast"])
        model_base = float(row["Base_Forecast"])
        tolerance = max(1e-6, abs(input_base) * 1e-6)
        if abs(model_base - input_base) > tolerance:
            raise ForecastOutputError(
                f"forecast[{index}].Base_Forecast changed from application input"
            )
        if row["New_Forecast"] <= 0:
            raise ForecastOutputError(f"forecast[{index}].New_Forecast must be > 0")
        if abs(row["adj_pct_total"]) > 10.0001:
            raise ForecastOutputError(
                f"forecast[{index}].adj_pct_total exceeds +/-10% guardrail"
            )
        if row["horizon_index"] != index:
            raise ForecastOutputError(
                f"forecast[{index}].horizon_index must equal row order {index}"
            )

    combined = payload.get("combined_signal") or {}
    _require_number(
        combined.get("total_adjust_pct_current"),
        "combined_signal.total_adjust_pct_current",
    )
    if abs(combined["total_adjust_pct_current"]) > 10.0001:
        raise ForecastOutputError("combined total adjustment exceeds +/-10%")

    return payload


def _select_current_forecast_row(forecast_rows, run_date):
    target_month = run_date.strftime("%Y-%m")
    for row in forecast_rows:
        try:
            parsed = datetime.strptime(row["Forecast_dates"], "%b-%y")
            if parsed.strftime("%Y-%m") == target_month:
                return row
        except (KeyError, TypeError, ValueError):
            continue
    return forecast_rows[0] if forecast_rows else None


def ai_forecast_output():
    """Run forecast calculation, narrative update and persistence for active commodities."""
    from helpers.ai_forecast_run_tracker import (
        ForecastRunTracker,
        score_correctness_c,
        score_faithfulness_f,
        score_faithfulness_g,
        score_faithfulness_h,
        usage_from_response,
    )

    logger.info("===== AI Forecast Job Started =====")
    tracker = ForecastRunTracker()
    original_create_openai_response = create_openai_response
    usage_by_call = {}

    def _capturing_create_openai_response(*args, **kwargs):
        response = original_create_openai_response(*args, **kwargs)
        call_name = kwargs.get("call_name", "unknown")
        usage_by_call[call_name] = usage_from_response(response)
        return response

    def _token_fields(usage):
        usage = usage or {}
        return {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }

    def _record_cp(
        *,
        cp_id,
        cp_name="",
        status="",
        format_ok="NO",
        correctness_C="",
        expected_direction="",
        detected_direction="",
        faithfulness_F="",
        faithfulness_G="",
        faithfulness_H="",
        forecast_status="",
        summary_status="",
        forecast_usage=None,
        summary_usage=None,
        news_dump_count="",
        price_url_count="",
        rows_saved="",
        errors=None,
        warnings=None,
    ):
        forecast_usage = _token_fields(forecast_usage)
        summary_usage = _token_fields(summary_usage)
        total_tokens = None
        if forecast_usage["total_tokens"] is not None or summary_usage["total_tokens"] is not None:
            total_tokens = (forecast_usage["total_tokens"] or 0) + (
                summary_usage["total_tokens"] or 0
            )

        error_text = "; ".join(errors or [])
        warning_text = "; ".join(warnings or [])
        tracker.add_row(
            cp_id=str(cp_id) if cp_id is not None else "",
            cp_name=cp_name or "",
            status=status,
            format_ok=format_ok,
            correctness_C=correctness_C,
            expected_direction=expected_direction,
            detected_direction=detected_direction,
            forecast_status=forecast_status,
            summary_status=summary_status,
            forecast_input_tokens=forecast_usage["input_tokens"],
            forecast_output_tokens=forecast_usage["output_tokens"],
            forecast_reasoning_tokens=forecast_usage["reasoning_tokens"],
            forecast_total_tokens=forecast_usage["total_tokens"],
            summary_input_tokens=summary_usage["input_tokens"],
            summary_output_tokens=summary_usage["output_tokens"],
            summary_reasoning_tokens=summary_usage["reasoning_tokens"],
            summary_total_tokens=summary_usage["total_tokens"],
            total_tokens=total_tokens,
            news_dump_count=news_dump_count,
            price_url_count=price_url_count,
            rows_saved=rows_saved,
            errors=error_text,
            warnings=warning_text,
        )

    globals()["create_openai_response"] = _capturing_create_openai_response

    try:
        cp_ids = list(
            LoadCPAIForecast.objects.filter(active=True)
            .values_list("cp_id", flat=True)
            .distinct()
        )
        if not cp_ids:
            logger.warning("No active commodities found")
            _record_cp(
                cp_id="",
                status="FAILED",
                forecast_status="SKIPPED",
                summary_status="SKIPPED",
                errors=["No active commodities found"],
            )
            return None

        client = get_openai_client()
        if not client:
            logger.error("Unable to initialize OpenAI client; aborting forecast job")
            _record_cp(
                cp_id="",
                status="FAILED",
                forecast_status="FAILED",
                summary_status="SKIPPED",
                errors=["Unable to initialize OpenAI client; aborting forecast job"],
            )
            return None

        all_outputs = {}
        logger.info("Found %s active CPs", len(cp_ids))

        for cp_id in cp_ids:
            logger.info("Starting forecast calculation for CP: %s", cp_id)
            errors = []
            warnings = []
            cp_name = ""
            forecast_usage = None
            summary_usage = None
            format_ok = "NO"
            correctness_C = ""
            expected_direction = ""
            detected_direction = ""
            faithfulness_F = ""
            faithfulness_G = ""
            faithfulness_H = ""
            forecast_status = ""
            summary_status = ""
            news_dump_count = ""
            price_url_count = ""
            rows_saved = ""

            try:
                summary_inputs = ai_summary_prompt_inputs(cp_id)
                if not summary_inputs:
                    logger.warning("No summary context for %s, skipping", cp_id)
                    errors.append("No summary context")
                    forecast_status = "SKIPPED"
                    summary_status = "SKIPPED"
                    _record_cp(
                        cp_id=cp_id,
                        status="FAILED",
                        format_ok=format_ok,
                        forecast_status=forecast_status,
                        summary_status=summary_status,
                        errors=errors,
                        warnings=warnings,
                    )
                    continue

                cp_name = summary_inputs.get("commodity_name") or ""
                news_records = summary_inputs.get("news_records") or []
                if not news_records:
                    warnings.append("No unique news articles prepared")
                if summary_inputs.get("last_actual") in (None, ""):
                    warnings.append("Latest monthly actual is missing")

                user_inputs = get_data_from_base_forecast(
                    cp_id,
                    optional_news_dump=summary_inputs.get("news_articles", ""),
                    region=summary_inputs.get("region"),
                )
                if not user_inputs:
                    logger.warning("No baseline data for %s, skipping", cp_id)
                    errors.append("No baseline data")
                    forecast_status = "SKIPPED"
                    summary_status = "SKIPPED"
                    _record_cp(
                        cp_id=cp_id,
                        cp_name=cp_name,
                        status="FAILED",
                        format_ok=format_ok,
                        forecast_status=forecast_status,
                        summary_status=summary_status,
                        errors=errors,
                        warnings=warnings,
                    )
                    continue

                final_prompt = AI_FORECAST_PROMPT.replace(
                    "<<USER_INPUTS_JSON>>",
                    json.dumps(user_inputs, ensure_ascii=False, separators=(",", ":")),
                )

                logger.info(
                    "Forecast prompt diagnostics | cp_id=%s | prompt_chars=%s | "
                    "template_chars=%s | user_inputs_chars=%s | baseline_rows=%s | "
                    "news_chars=%s",
                    cp_id,
                    len(final_prompt),
                    len(AI_FORECAST_PROMPT),
                    len(json.dumps(user_inputs, ensure_ascii=False, separators=(",", ":"))),
                    len(user_inputs.get("Baseline_Forecast_Rows", [])),
                    len(user_inputs.get("Optional_News_Dump", "")),
                )

                logger.info(
                    "Forecast OpenAI config | cp_id=%s model=%s reasoning=%s "
                    "max_output_tokens=%s verbosity=%s web_search=%s search_context=%s",
                    cp_id,
                    MODEL_NAME,
                    FORECAST_REASONING_LEVEL,
                    FORECAST_MAX_OUTPUT_TOKENS,
                    "medium",
                    True,
                    "medium",
                )

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
                )
                forecast_usage = usage_by_call.get(f"forecast:{cp_id}")

                if not response:
                    logger.error("OpenAI forecast response failed for %s", cp_id)
                    errors.append("OpenAI forecast response failed")
                    forecast_status = "FAILED"
                    summary_status = "SKIPPED"
                    _record_cp(
                        cp_id=cp_id,
                        cp_name=cp_name,
                        status="FAILED",
                        format_ok=format_ok,
                        forecast_status=forecast_status,
                        summary_status=summary_status,
                        forecast_usage=forecast_usage,
                        errors=errors,
                        warnings=warnings,
                    )
                    continue

                prompt_one_output = assemble_forecast_output(
                    user_inputs, _parse_json_object(response.output_text)
                )
                validate_forecast_output(
                    prompt_one_output,
                    baseline_rows=user_inputs["Baseline_Forecast_Rows"],
                    run_context=user_inputs["Run_Context"],
                )
                format_ok = "YES"
                forecast_status = "PASSED"

                faithfulness_H, h_note, news_url_count, price_urls = score_faithfulness_h(
                    prompt_one_output
                )
                news_dump = prompt_one_output.get("news_dump") or []
                news_dump_count = len(news_dump) if isinstance(news_dump, list) else 0
                price_url_count = price_urls
                if h_note:
                    warnings.append(h_note)

                current_row = _select_current_forecast_row(
                    prompt_one_output["forecast"], timezone.localdate()
                )
                if not current_row:
                    logger.error("No forecast row returned for %s", cp_id)
                    errors.append("No forecast row returned")
                    summary_status = "SKIPPED"
                    _record_cp(
                        cp_id=cp_id,
                        cp_name=cp_name,
                        status="FAILED",
                        format_ok=format_ok,
                        forecast_status=forecast_status,
                        summary_status=summary_status,
                        forecast_usage=forecast_usage,
                        news_dump_count=news_dump_count,
                        price_url_count=price_url_count,
                        errors=errors,
                        warnings=warnings,
                    )
                    continue

                base_forecast = current_row["Base_Forecast"]
                new_forecast = current_row["New_Forecast"]

                summary = generate_ai_sense(
                    client=client,
                    cp_id=cp_id,
                    base_forecast=base_forecast,
                    new_forecast=new_forecast,
                    summary_inputs=summary_inputs,
                )
                summary_usage = usage_by_call.get(f"summary:{cp_id}")

                original_analysis = strip_source_appendix(
                    summary_inputs.get("base_analysis") or ""
                )
                replacement_bullet = ""

                if not summary:
                    summary_status = "FAILED"
                    errors.append("Summary generation returned empty output")
                    correctness_C = "N/A"
                    faithfulness_F = "FAIL"
                    faithfulness_G = "N/A"
                else:
                    try:
                        original_bullet, bullet_start, bullet_end = (
                            _extract_first_short_term_bullet(original_analysis)
                        )
                        replacement_bullet, _, _ = _extract_first_short_term_bullet(summary)
                        correctness_C, expected_direction, detected_direction, c_note = (
                            score_correctness_c(
                                summary_inputs.get("last_actual"),
                                new_forecast,
                                replacement_bullet,
                            )
                        )
                        if c_note:
                            warnings.append(c_note)

                        faithfulness_F, f_note = score_faithfulness_f(
                            original_analysis,
                            summary,
                            bullet_start,
                            bullet_end,
                        )
                        if f_note:
                            warnings.append(f_note)

                        faithfulness_G, g_note = score_faithfulness_g(
                            expected_direction,
                            replacement_bullet,
                        )
                        if g_note:
                            warnings.append(g_note)

                        if summary.strip() == original_analysis.strip():
                            summary_status = "PRESERVED"
                            warnings.append("Original analysis preserved")
                        else:
                            summary_status = "UPDATED"
                    except ForecastOutputError as summary_eval_exc:
                        summary_status = "FAILED"
                        errors.append(str(summary_eval_exc))
                        correctness_C = correctness_C or "N/A"
                        faithfulness_F = faithfulness_F or "FAIL"
                        faithfulness_G = faithfulness_G or "N/A"

                all_outputs[cp_id] = {
                    "inputs": user_inputs,
                    "forecast": prompt_one_output,
                    "summary": summary,
                }

                count = bulk_save_ai_forecast(
                    all_outputs[cp_id], created_by="AI Forecast Script"
                )
                rows_saved = count
                logger.info("Saved %s AI forecast rows for CP: %s", count, cp_id)

                status = "SUCCESS"
                if errors:
                    status = "FAILED"
                elif (
                    "FAIL" in {correctness_C, faithfulness_F, faithfulness_G, faithfulness_H}
                    or warnings
                    or correctness_C == "UNCLEAR"
                    or faithfulness_G == "UNCLEAR"
                ):
                    status = "WARNING"

                _record_cp(
                    cp_id=cp_id,
                    cp_name=cp_name,
                    status=status,
                    format_ok=format_ok,
                    correctness_C=correctness_C,
                    expected_direction=expected_direction,
                    detected_direction=detected_direction,
                    forecast_status=forecast_status,
                    summary_status=summary_status,
                    forecast_usage=forecast_usage,
                    summary_usage=summary_usage,
                    news_dump_count=news_dump_count,
                    price_url_count=price_url_count,
                    rows_saved=rows_saved,
                    errors=errors,
                    warnings=warnings,
                )

            except ForecastOutputError as exc:
                logger.error("Forecast output validation failed for %s: %s", cp_id, exc)
                errors.append(str(exc))
                forecast_status = forecast_status or "FAILED"
                summary_status = summary_status or "SKIPPED"
                _record_cp(
                    cp_id=cp_id,
                    cp_name=cp_name,
                    status="FAILED",
                    format_ok=format_ok,
                    correctness_C=correctness_C,
                    expected_direction=expected_direction,
                    detected_direction=detected_direction,
                    forecast_status=forecast_status,
                    summary_status=summary_status,
                    forecast_usage=forecast_usage or usage_by_call.get(f"forecast:{cp_id}"),
                    summary_usage=summary_usage,
                    news_dump_count=news_dump_count,
                    price_url_count=price_url_count,
                    rows_saved=rows_saved,
                    errors=errors,
                    warnings=warnings,
                )
            except Exception as exc:
                logger.exception("Fatal error while processing %s", cp_id)
                errors.append(str(exc))
                forecast_status = forecast_status or "FAILED"
                summary_status = summary_status or "SKIPPED"
                _record_cp(
                    cp_id=cp_id,
                    cp_name=cp_name,
                    status="FAILED",
                    format_ok=format_ok,
                    forecast_status=forecast_status,
                    summary_status=summary_status,
                    forecast_usage=forecast_usage or usage_by_call.get(f"forecast:{cp_id}"),
                    summary_usage=summary_usage or usage_by_call.get(f"summary:{cp_id}"),
                    news_dump_count=news_dump_count,
                    price_url_count=price_url_count,
                    rows_saved=rows_saved,
                    errors=errors,
                    warnings=warnings,
                )

        logger.info("===== AI Forecast Job Finished =====")
        return all_outputs

    finally:
        globals()["create_openai_response"] = original_create_openai_response
        tracker.flush()


def fetch_commodity_news(cp_id):
    """Fetch recent commodity news from the internal news API."""
    try:
        if settings.MODE in {"DEV1", "QA", "TESTING"}:
            category_news_api = getattr(
                settings,
                "AI_FORECAST_NEWS_API_URL_NONPROD",
                "http://127.0.0.1:8000/api/v1.0/news/getCommodityNews",
            )
        else:
            category_news_api = category_new_api_start_point_PROD + category_new_api_end_point

        token = _setting_or_env("COMMODITY_NEWS_TOKEN")
        user_id = _setting_or_env("COMMODITY_NEWS_USER_ID")
        if not token or not user_id:
            logger.error(
                "Commodity news credentials are missing; configure COMMODITY_NEWS_TOKEN "
                "and COMMODITY_NEWS_USER_ID"
            )
            return {}

        headers = {
            "Content-Type": "application/json",
            "Token": str(token),
            "UserId": str(user_id),
        }

        modified_end_date = timezone.localtime()
        modified_start_date = modified_end_date - relativedelta(hours=NEWS_LOOKBACK_HOURS)

        payload = {
            "modified_start_date": modified_start_date.strftime("%Y-%m-%d %H:%M:%S"),
            "modified_end_date": modified_end_date.strftime("%Y-%m-%d %H:%M:%S"),
            "commodity_id": cp_id,
            "limit": NEWS_API_LIMIT,
        }

        logger.info(
            "News API range %s -> %s | cp_id=%s limit=%s",
            payload["modified_start_date"],
            payload["modified_end_date"],
            cp_id,
            NEWS_API_LIMIT,
        )

        try:
            response = requests.post(
                category_news_api,
                headers=headers,
                json=payload,
                verify=False,
                timeout=int(getattr(settings, "AI_FORECAST_NEWS_TIMEOUT", 20)),
            )
            response.raise_for_status()
        except requests.exceptions.Timeout:
            logger.error("News API timeout for cp_id=%s", cp_id)
            return {}
        except requests.exceptions.RequestException as exc:
            logger.error("News API request failed for cp_id=%s | %s", cp_id, exc)
            return {}

        try:
            result = response.json()
        except ValueError:
            logger.error("Invalid JSON received from News API for cp_id=%s", cp_id)
            return {}

        if not isinstance(result, dict):
            logger.warning("Unexpected news API schema for cp_id=%s", cp_id)
            return {}
        return result

    except Exception:
        logger.exception("Critical failure in fetch_commodity_news for cp_id=%s", cp_id)
        return {}


def _news_sort_key(item):
    date_value = item.get("date") or ""
    try:
        return date_parser.parse(date_value).date().toordinal()
    except (TypeError, ValueError, OverflowError):
        return 0


def _format_news_articles(news_response):
    response_items = news_response.get("response", []) if isinstance(news_response, dict) else []
    if not isinstance(response_items, list):
        return "", []

    seen = set()
    unique_news = []
    for item in response_items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        summary = str(item.get("description") or "").strip()
        date = str(item.get("date_published") or "").strip()
        if not title or not summary:
            continue

        normalized_title = re.sub(r"\s+", " ", title).casefold()
        unique_key = (normalized_title, date)
        if unique_key in seen:
            continue
        seen.add(unique_key)
        unique_news.append({"title": title, "summary": summary, "date": date})

    unique_news.sort(key=_news_sort_key, reverse=True)
    unique_news = unique_news[:MAX_NEWS_ARTICLES]

    blocks = []
    for index, item in enumerate(unique_news, start=1):
        blocks.append(
            f"{index}) {item['title']}\n"
            f"Summary: {item['summary']}\n"
            f"Date: {item['date']}"
        )
    return "\n\n".join(blocks), unique_news


def _find_latest_base_analysis(cp_id):
    base_summary = (
        LoadCPAIForecast.objects.filter(
            cp_id=cp_id,
            active=True,
            base_forecast_flag=True,
        )
        .exclude(summary__isnull=True)
        .exclude(summary="")
        .order_by("-modified_at")
        .values_list("summary", flat=True)
        .first()
    )
    if base_summary:
        return base_summary

    return (
        LoadCPAIForecast.objects.filter(
            cp_id=cp_id,
            active=True,
            ai_forecast_flag=True,
        )
        .exclude(summary__isnull=True)
        .exclude(summary="")
        .order_by("-modified_at")
        .values_list("summary", flat=True)
        .first()
    )


def ai_summary_prompt_inputs(cp_id):
    """Fetch the existing analysis, latest actual, commodity metadata and capped news once."""
    try:
        logger.info("Preparing AI summary inputs for cp_id=%s", cp_id)

        forecast_row = (
            LoadCPAIForecast.objects.filter(cp_id=cp_id, active=True)
            .order_by("-base_forecast_flag", "-ai_forecast_flag", "-modified_at")
            .first()
        )
        if not forecast_row:
            logger.warning("No forecast data found for cp_id=%s", cp_id)
            return {}

        base_analysis = _find_latest_base_analysis(cp_id) or ""

        cp_value = (
            LoadCP.objects.filter(
                subasset_id=cp_id,
                data_freq="Monthly",
                active=True,
            )
            .order_by("-modified_at")
            .values_list("cp_value", flat=True)
            .first()
        )
        if cp_value is None:
            logger.warning("No latest monthly actual found for cp_id=%s", cp_id)

        news_response = fetch_commodity_news(cp_id)
        news_articles, news_records = _format_news_articles(news_response)
        logger.info(
            "Prepared %s unique news articles for cp_id=%s",
            len(news_records),
            cp_id,
        )

        return {
            "commodity_name": forecast_row.cp_name or "Unknown",
            "region": _resolve_region(forecast_row),
            "last_actual": cp_value,
            "news_articles": news_articles,
            "news_records": news_records,
            "base_analysis": base_analysis,
        }

    except DatabaseError as exc:
        logger.error("Database error for cp_id=%s | %s", cp_id, exc, exc_info=True)
        return {}
    except Exception as exc:
        logger.error("Unexpected summary-input error for cp_id=%s | %s", cp_id, exc, exc_info=True)
        return {}


def strip_source_appendix(analysis):
    """
    Remove a trailing numbered source/news appendix only when it has the characteristic
    '<number>. <title>' followed by 'Summary:' structure. This targets legacy polluted
    summaries without broadly deleting numbered narrative content.
    """
    text = analysis or ""
    long_term_match = re.search(r"(?im)^\s*Long-term Outlook\s*$", text)
    search_start = long_term_match.end() if long_term_match else 0
    tail = text[search_start:]

    appendix_match = re.search(
        r"(?im)^\s*\d+[.)]\s+[^\n]+\n\s*Summary:\s*.+$",
        tail,
    )
    if not appendix_match:
        return text

    cut_at = search_start + appendix_match.start()
    cleaned = text[:cut_at].rstrip()
    logger.warning("Removed legacy source appendix from stored base analysis")
    return cleaned


def _extract_first_short_term_bullet(analysis):
    """Return (original_bullet, start_index, end_index) for the first Short-term Ω bullet only."""
    text = analysis or ""

    short_match = re.search(
        r"(?im)^\s*\*{0,2}Short-term Outlook\*{0,2}\s*:?",
        text,
    )
    if not short_match:
        raise ForecastOutputError("BASE_ANALYSIS is missing 'Short-term Outlook'")

    section_start = short_match.end()
    long_match = re.search(
        r"(?im)^\s*\*{0,2}Long-term Outlook\*{0,2}\s*:?",
        text[section_start:],
    )
    section_end = section_start + long_match.start() if long_match else len(text)
    section = text[section_start:section_end]

    # Only the first Ω headline after Short-term Outlook. π driver bullets and later
    # Ω bullets stay untouched and are not passed to the summary prompt.
    omega_match = re.search(r"Ω[^\r\n]*", section)
    if omega_match:
        bullet_start = section_start + omega_match.start()
        bullet_end = section_start + omega_match.end()
        original = text[bullet_start:bullet_end].strip()
        if not original:
            raise ForecastOutputError("Short-term Outlook first bullet is empty")
        return original, bullet_start, bullet_end

    bullet_start = section_start
    while bullet_start < section_end and text[bullet_start].isspace():
        bullet_start += 1

    line_end = text.find("\n", bullet_start, section_end)
    bullet_end = line_end if line_end != -1 else section_end
    while bullet_end > bullet_start and text[bullet_end - 1].isspace():
        bullet_end -= 1

    original = text[bullet_start:bullet_end].strip()
    if not original:
        raise ForecastOutputError("Short-term Outlook section is empty")
    return original, bullet_start, bullet_end


def _validate_replacement_bullet(replacement, original_bullet):
    text = (replacement or "").strip()
    if not text:
        raise ForecastOutputError("Summary replacement bullet is empty")

    forbidden = (
        "Short-term Outlook",
        "Long-term Outlook",
        "Commodity:",
        "Region:",
        "Summary:",
        "http://",
        "https://",
    )
    if any(token.casefold() in text.casefold() for token in forbidden):
        raise ForecastOutputError("Summary replacement contains forbidden headings/source material")

    if len(text.split()) > MAX_SUMMARY_BULLET_WORDS:
        raise ForecastOutputError(
            f"Summary replacement exceeds {MAX_SUMMARY_BULLET_WORDS} words"
        )

    marker_match = re.match(r"^(\s*(?:[-*•]|\d+[.)])\s+)", original_bullet)
    if marker_match and not re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", text):
        text = marker_match.group(1) + text

    if original_bullet.lstrip().startswith("Ω") and not text.lstrip().startswith("Ω"):
        leading = re.match(r"^\s*", original_bullet).group(0)
        text = leading + "Ω" + text.lstrip()

    return text


def generate_ai_sense(
    client,
    cp_id,
    base_forecast,
    new_forecast,
    summary_inputs=None,
):
    """Generate only the changed short-term Ω bullet and merge it into the existing analysis."""
    logger.info("Generating AI sense for cp_id=%s", cp_id)
    inputs = summary_inputs or ai_summary_prompt_inputs(cp_id)
    if not inputs:
        return ""

    base_analysis = strip_source_appendix(inputs.get("base_analysis") or "")
    if not base_analysis:
        logger.error("No BASE_ANALYSIS available for cp_id=%s", cp_id)
        return ""

    try:
        logger.info(
            "BASE_ANALYSIS DEBUG | cp_id=%s | repr=%r",
            cp_id,
            base_analysis,
        )
        original_bullet, bullet_start, bullet_end = _extract_first_short_term_bullet(
            base_analysis
        )
        logger.info(
            "Summary prompt original bullet | cp_id=%s | bullet=%r",
            cp_id,
            original_bullet,
        )

    except ForecastOutputError as exc:
        logger.error("Cannot update BASE_ANALYSIS for cp_id=%s: %s", cp_id, exc)
        return base_analysis

    last_actual = inputs.get("last_actual")
    if last_actual in (None, ""):
        logger.warning(
            "Latest actual missing for cp_id=%s; preserving original narrative", cp_id
        )
        return base_analysis

    final_prompt = (
        AI_SUMMARY_PROMPT.replace("<<NEWS_ARTICLES>>", inputs.get("news_articles") or "None supplied")
        .replace("<<ORIGINAL_BULLET>>", original_bullet)
        .replace("<<COMMODITY_NAME>>", str(inputs.get("commodity_name") or ""))
        .replace("<<REGION>>", str(inputs.get("region") or "Global"))
        .replace("<<LAST_ACTUAL>>", str(last_actual))
        .replace("<<PREVIOUS_FORECAST>>", str(base_forecast))
        .replace("<<REVISED_FORECAST>>", str(new_forecast))
    )

    response = create_openai_response(
        client=client,
        final_prompt=final_prompt,
        MODEL_NAME=MODEL_NAME,
        REASONING_LEVEL=SUMMARY_REASONING_LEVEL,
        verbosity="medium",
        enable_web_search=True,
        search_context_size="medium",
        call_name=f"summary:{cp_id}",
    )
    if not response:
        logger.error("Summary generation failed; preserving base analysis | cp_id=%s", cp_id)
        return base_analysis

    try:
        replacement = _validate_replacement_bullet(response.output_text, original_bullet)
    except ForecastOutputError as exc:
        logger.error(
            "Invalid summary replacement; preserving base analysis | cp_id=%s error=%s",
            cp_id,
            exc,
        )
        return base_analysis

    merged = base_analysis[:bullet_start] + replacement + base_analysis[bullet_end:]
    return merged.strip()


@transaction.atomic
def bulk_save_ai_forecast(cp_output, created_by="AI Forecast Script"):
    """Persist validated AI forecast rows and the merged narrative atomically."""
    inputs = cp_output["inputs"]
    forecast_data = cp_output["forecast"]
    summary_text = cp_output.get("summary") or ""
    new_summary_text = forecast_data.get("New_Summary") or ""

    run_context = inputs["Run_Context"]
    cp_id = run_context["commodity_id"]
    cp_name = run_context["commodity_name"]
    unit_name = run_context["unit_name"]
    month_of_data_received = run_context["month_label"]
    forecast_rows = forecast_data["forecast"]
    now = timezone.now()

    existing_ai_qs = LoadCPAIForecast.objects.filter(
        cp_id=cp_id,
        ai_forecast_flag=True,
        active=True,
    )
    existing_base_qs = LoadCPAIForecast.objects.filter(
        cp_id=cp_id,
        base_forecast_flag=True,
        active=True,
    )

    saved_count = 0

    if existing_ai_qs.exists():
        desired_timeframes = []
        for row in forecast_rows:
            timeframe = convert_forecast_month_to_date(row["Forecast_dates"])
            if not timeframe:
                logger.warning(
                    "Skipping invalid forecast timeframe during update | cp_id=%s value=%r",
                    cp_id,
                    row.get("Forecast_dates"),
                )
                continue
            desired_timeframes.append(timeframe)

            updated = LoadCPAIForecast.objects.filter(
                cp_id=cp_id,
                timeframe=timeframe,
                ai_forecast_flag=True,
                active=True,
            ).update(
                forecast_price=str(row["New_Forecast"]),
                da_forecast=str(row["Base_Forecast"]),
                unit_name=unit_name,
                month_of_data_received=month_of_data_received,
                summary=summary_text,
                new_summary=new_summary_text,
                is_delete_flag=False,
                modified_by=created_by,
                modified_at=now,
            )

            if updated:
                saved_count += updated
                continue

            LoadCPAIForecast.objects.create(
                cp_id=cp_id,
                cp_name=cp_name,
                timeframe=timeframe,
                forecast_price=str(row["New_Forecast"]),
                da_forecast=str(row["Base_Forecast"]),
                unit_name=unit_name,
                month_of_data_received=month_of_data_received,
                summary=summary_text,
                new_summary=new_summary_text,
                is_original=False,
                base_forecast_flag=False,
                is_delete_flag=True,
                ai_forecast_flag=True,
                active=True,
                created_by=created_by,
                modified_by=created_by,
                created_at=now,
                modified_at=now,
            )
            saved_count += 1

        if desired_timeframes:
            retired = existing_ai_qs.exclude(timeframe__in=desired_timeframes).update(
                active=False,
                is_delete_flag=True,
                modified_by=created_by,
                modified_at=now,
            )
            if retired:
                logger.info(
                    "Retired %s stale AI forecast rows for CP: %s", retired, cp_id
                )

        logger.info("AI rows upserted for CP: %s", cp_id)
        return saved_count

    if existing_base_qs.exists():
        existing_base_qs.update(
            base_forecast_flag=False,
            is_delete_flag=True,
            modified_by=created_by,
            modified_at=now,
        )

    objects_to_create = []
    for row in forecast_rows:
        timeframe = convert_forecast_month_to_date(row["Forecast_dates"])
        if not timeframe:
            logger.warning(
                "Skipping invalid forecast timeframe during insert | cp_id=%s value=%r",
                cp_id,
                row.get("Forecast_dates"),
            )
            continue

        objects_to_create.append(
            LoadCPAIForecast(
                cp_id=cp_id,
                cp_name=cp_name,
                timeframe=timeframe,
                forecast_price=str(row["New_Forecast"]),
                da_forecast=str(row["Base_Forecast"]),
                unit_name=unit_name,
                month_of_data_received=month_of_data_received,
                summary=summary_text,
                new_summary=new_summary_text,
                is_original=False,
                base_forecast_flag=False,
                is_delete_flag=True,
                ai_forecast_flag=True,
                active=True,
                created_by=created_by,
                modified_by=created_by,
                created_at=now,
                modified_at=now,
            )
        )

    LoadCPAIForecast.objects.bulk_create(objects_to_create)
    logger.info("AI rows inserted for CP: %s", cp_id)
    return len(objects_to_create)
