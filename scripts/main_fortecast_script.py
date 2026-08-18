"""Drop-in AI forecast job with first-bullet extraction for the summary prompt.

Copy into scripts/main_fortecast_script.py in the Django app. Unchanged helpers
(get_data_from_base_forecast, fetch_commodity_news, bulk_save_ai_forecast,
ai_forecast_output) stay as in production; this file includes them so the
wiring is complete.
"""

import json
import requests
from django.conf import settings
from scripts.forecast_prompt import AI_FORECAST_PROMPT, AI_SUMMARY_PROMPT
from helpers.config import category_new_api_start_point_PROD, category_new_api_end_point
from datetime import datetime
from dateutil.relativedelta import relativedelta
from apps.commodity_price.models import LoadCPAIForecast
from django.utils import timezone
from helpers.config import base_forecast_logger as logger
from django.db import DatabaseError
from apps.load_layer.models import LoadCP
from helpers.ai_forecast_helper import get_openai_client, create_openai_response, convert_forecast_month_to_date
from helpers.forecast_text_utils import (
    build_ai_summary_prompt,
    get_first_short_term_bullet_for_prompt,
)

MODEL_NAME = "gpt-5.4-mini"
REASONING_LEVEL = "medium"


def get_data_from_base_forecast(cp_id):
    """
    Retrieves active ai forecast records for a given commodity ID,
    formats forecast dates and prices, fetches related AI news data,
    and returns a structured dictionary for further processing.

    Returns an empty dictionary if no active records are found.
    """
    qs = LoadCPAIForecast.objects.filter(cp_id=cp_id, active=True, base_forecast_flag=True)

    if not qs.exists():
        qs = LoadCPAIForecast.objects.filter(cp_id=cp_id, active=True, ai_forecast_flag=True)

    if not qs.exists():
        return {}

    first_row = qs.first()

    baseline_rows = []
    news_dump = ai_summary_prompt_inputs(cp_id)
    optional_news_dump = news_dump.get("news_articles", "")
    for row in qs:
        if row.timeframe:
            timeframe = row.timeframe.strftime("%b-%y")
        else:
            timeframe = ""

        baseline_rows.append({
            "forecast_dates": timeframe,
            "Base_Forecast": float(row.da_forecast)
        })

    return {
        "Run_Context": {
            "run_date_local": datetime.now().strftime("%Y-%m-%d"),
            "commodity_id": first_row.cp_id,
            "commodity_name": first_row.cp_name,
            "unit_name": first_row.unit_name,
            "region": "Global",
            "month_label": datetime.now().strftime("%b-%Y")
        },
        "Baseline_Forecast_Rows": baseline_rows,
        "Optional_News_Dump": optional_news_dump
    }


def ai_forecast_output():
    """
    Runs the AI forecasting pipeline for all active commodities by
    preparing inputs, calling the AI model, processing responses,
    saving results to the database, and exporting outputs to JSON.
    """

    logger.info("===== AI Forecast Job Started =====")
    cp_ids = (LoadCPAIForecast.objects.filter(active=True).values_list("cp_id", flat=True).distinct())

    if not cp_ids:
        logger.warning("No active commodities found.")
        return None

    all_outputs = {}

    logger.info("Found %s active CPs", len(cp_ids))

    for cp_id in cp_ids:
        client = get_openai_client()
        logger.info("Starting forecast calculation for CP: %s", cp_id)

        try:
            user_inputs = get_data_from_base_forecast(cp_id)
            if not user_inputs:
                logger.warning("No baseline data for %s, skipping", cp_id)
                continue

            final_prompt = AI_FORECAST_PROMPT.replace(
                "<<USER_INPUTS_JSON>>",
                json.dumps(user_inputs, indent=2)
            )

            response = create_openai_response(
                client=client,
                final_prompt=final_prompt,
                MODEL_NAME=MODEL_NAME,
                REASONING_LEVEL=REASONING_LEVEL,
                operation_name="FORECAST"
            )
            if not response:
                logger.error("OpenAI response failed for %s", cp_id)
                continue
            raw_forecast_output = response.output_text
            try:
                prompt_one_output = json.loads(raw_forecast_output)
            except json.JSONDecodeError as e:
                logger.error("Invalid JSON from AI for %s: %s", cp_id, e)
                logger.debug(raw_forecast_output)
                continue

            base_forecast = prompt_one_output["forecast"][0]["Base_Forecast"]
            new_forecast = prompt_one_output["forecast"][0]["New_Forecast"]
            logger.info(
                "[FORECAST REVISION] cp_id=%s | previous_forecast(base)=%s | revised_forecast(new)=%s",
                cp_id,
                base_forecast,
                new_forecast,
            )

            summary = generate_ai_sense(client, cp_id, base_forecast, new_forecast)

            all_outputs[cp_id] = {
                "inputs": user_inputs,
                "forecast": prompt_one_output,
                "summary": summary
            }
            logger.info("AI calculated for %s", cp_id)

            count = bulk_save_ai_forecast(
                all_outputs[cp_id],
                created_by="AI Forecast Script"
            )
            logger.info("Inserted %s rows for %s", count, cp_id)

        except Exception:
            logger.exception("Fatal error while processing %s", cp_id)
            continue
    logger.info("===== AI Forecast Job Finished =====")
    return all_outputs


def fetch_commodity_news(cp_id):
    """
    Fetch commodity news for a given commodity ID.
    Date range: Last 2 days till current time.
    """

    try:

        if settings.MODE in ["QA", "TESTING"]:
            logger.info("Commodity News API call from DEV/QA environment")
            category_news_api = "http://127.0.0.1:8000/api/v1.0/news/getCommodityNews"
        else:
            logger.info("Commodity News API call from PROD environment")
            category_news_api = category_new_api_start_point_PROD + category_new_api_end_point

        headers = {
            "Content-Type": "application/json",
            "Token": "dZrzmXxPyK9Krr-20200207-055202",
            "UserId": "2740"
        }

        modified_end_date = datetime.now()
        modified_start_date = modified_end_date - relativedelta(hours=4)

        modified_end_date_str = modified_end_date.strftime("%Y-%m-%d %H:%M:%S")
        modified_start_date_str = modified_start_date.strftime("%Y-%m-%d %H:%M:%S")

        logger.info(
            "News API date range: %s → %s for cp_id: %s",
            modified_start_date_str,
            modified_end_date_str,
            cp_id,
        )
        payload = json.dumps({
            "modified_start_date": modified_start_date_str,
            "modified_end_date": modified_end_date_str,
            "commodity_id": cp_id,
            "limit": 5000
        })

        try:
            response = requests.post(category_news_api, headers=headers, data=payload, verify=False, timeout=20)
        except requests.exceptions.Timeout:
            logger.error("News API timeout for cp_id: %s", cp_id)
            return ""
        except requests.exceptions.ConnectionError:
            logger.error("News API connection error for cp_id: %s", cp_id)
            return ""
        except Exception:
            logger.exception("Unexpected request failure for cp_id: %s", cp_id)
            return ""
        logger.info("News API status code: %s for cp_id: %s", response.status_code, cp_id)

        if response.status_code != 200:
            logger.warning(
                "Non-200 response from News API (%s) for cp_id: %s",
                response.status_code,
                cp_id,
            )
            return ""

        try:
            return response.json()
        except ValueError:
            logger.error("Invalid JSON received from News API for cp_id: %s", cp_id)
            return ""
    except Exception:
        logger.exception("Critical failure in fetch_commodity_news for cp_id: %s", cp_id)
        return ""


def ai_summary_prompt_inputs(cp_id):
    """
    Fetch commodity details, previous month value,
    summary, and latest news. Also extract the first Short-term Outlook
    bullet from summary / base analysis for the summary prompt.
    """

    try:
        logger.info("Starting AI Sense analysis for cp_id=%s", cp_id)

        forecast_data = (
            LoadCPAIForecast.objects
            .filter(cp_id=cp_id, active=True)
            .values(
                "cp_name",
                "summary",
                "base_forecast_flag",
                "ai_forecast_flag"
            )
        )

        forecast_list = list(forecast_data)
        if forecast_list:
            commodity_name = forecast_list[0].get("cp_name", "Unknown")
        else:
            commodity_name = "Unknown"
            logger.warning("No forecast data found for cp_id=%s", cp_id)

        base_analysis = None
        base_analysis_source = None

        for row in forecast_list:
            if row["base_forecast_flag"]:
                base_analysis = row["summary"]
                base_analysis_source = "base_forecast.summary"
                break
        if base_analysis is None:
            for row in forecast_list:
                if row["ai_forecast_flag"]:
                    base_analysis = row["summary"]
                    base_analysis_source = "ai_forecast.summary"
                    break

        logger.info(
            "[BASE ANALYSIS SOURCE] cp_id=%s | source=%s | chars=%s",
            cp_id,
            base_analysis_source or "none",
            len(base_analysis or ""),
        )

        first_short_term_bullet = get_first_short_term_bullet_for_prompt(
            base_analysis or "",
            log=logger,
            cp_id=cp_id,
        )

        cp_value = (
            LoadCP.objects
            .filter(
                subasset_id=cp_id,
                data_freq="Monthly",
                active=True,
            )
            .order_by("-modified_at")
            .values_list("cp_value", flat=True)
            .first()
        )

        if cp_value is None:
            logger.warning("No CP value found for cp_id=%s", cp_id)
        news_articles = ""

        try:
            news_response = fetch_commodity_news(cp_id=cp_id)
            if news_response and "response" in news_response:

                seen_articles = set()
                unique_news = []

                for item in news_response["response"]:

                    title = item.get("title", "").strip()
                    summary = item.get("description", "").strip()
                    date = item.get("date_published", "").strip()

                    unique_key = f"{title}|{date}"

                    if unique_key in seen_articles:
                        continue

                    seen_articles.add(unique_key)

                    unique_news.append({
                        "title": title,
                        "summary": summary,
                        "date": date
                    })
                for i, news in enumerate(unique_news, start=1):
                    news_articles += f"""
                    {i}) {news['title']}
                    Summary: {news['summary']}
                    Date: {news['date']}
                    """
                logger.info("News fetched for cp_id=%s", cp_id)
            else:
                logger.warning("Empty news response for cp_id=%s", cp_id)
        except Exception as news_error:
            logger.error(
                "News fetch failed for cp_id=%s | %s",
                cp_id,
                str(news_error),
                exc_info=True,
            )

        logger.info("Generating AI summary inputs for cp_id=%s", cp_id)
        return {
            "commodity_name": commodity_name,
            "region": "Global",
            "last_actual": cp_value,
            "news_articles": news_articles,
            "base_articles": base_analysis,
            "first_short_term_bullet": first_short_term_bullet,
        }
    except DatabaseError as db_error:
        logger.error("Database error for cp_id=%s | %s", cp_id, str(db_error), exc_info=True)
        return {}
    except Exception as error:
        logger.error("Unexpected error for cp_id=%s | %s", cp_id, str(error), exc_info=True)
        return {}


def generate_ai_sense(client, cp_id, base_forecast, new_forecast):
    """Build the summary prompt, injecting the extracted first short-term bullet."""

    logger.info("Generating AI sense for cp_id=%s", cp_id)
    inputs = ai_summary_prompt_inputs(cp_id)

    news_articles = inputs.get("news_articles") or ""
    base_articles = inputs.get("base_articles") or ""
    logger.info(
        "[BASE ANALYSIS] cp_id=%s | chars=%s | words=%s",
        cp_id,
        len(base_articles),
        len(base_articles.split()),
    )
    logger.info(
        "[BASE ANALYSIS END] cp_id=%s | last_1000=%s",
        cp_id,
        base_articles[-1000:],
    )
    commodity_name = inputs.get("commodity_name") or ""
    region = inputs.get("region") or ""
    last_actual = inputs.get("last_actual") or ""

    first_short_term_bullet = inputs.get("first_short_term_bullet")
    if first_short_term_bullet is None:
        logger.warning(
            "[FIRST BULLET] cp_id=%s | missing on inputs; extracting again from base analysis",
            cp_id,
        )
        first_short_term_bullet = get_first_short_term_bullet_for_prompt(
            base_articles,
            log=logger,
            cp_id=cp_id,
        )

    logger.info(
        "[SUMMARY PLACEHOLDERS] cp_id=%s | commodity=%s | region=%s | last_actual=%s | previous_forecast=%s | revised_forecast=%s | first_bullet_chars=%s",
        cp_id,
        commodity_name,
        region,
        last_actual,
        base_forecast,
        new_forecast,
        len(first_short_term_bullet or ""),
    )

    final_prompt = build_ai_summary_prompt(
        AI_SUMMARY_PROMPT,
        news_articles=news_articles,
        base_articles=base_articles,
        commodity_name=commodity_name,
        region=region,
        last_actual=last_actual,
        previous_forecast=base_forecast,
        revised_forecast=new_forecast,
        first_short_term_bullet=first_short_term_bullet,
        log=logger,
        cp_id=cp_id,
    )

    response = create_openai_response(
        client=client,
        final_prompt=final_prompt,
        MODEL_NAME=MODEL_NAME,
        REASONING_LEVEL=REASONING_LEVEL,
        operation_name="SUMMARY"
    )
    if not response:
        return ""

    summary = response.output_text

    logger.info(
        "[SUMMARY OUTPUT] cp_id=%s | chars=%s | words=%s",
        cp_id,
        len(summary),
        len(summary.split()),
    )
    logger.info(
        "[SUMMARY OUTPUT END] cp_id=%s | last_500_chars=%s",
        cp_id,
        summary[-500:],
    )
    return summary


def bulk_save_ai_forecast(cp_output, created_by="AI Forecast Script"):
    """
    Saves AI forecast results into the database by overwriting existing AI records,
    converting base forecasts to AI forecasts, or inserting new records,
    depending on the current state of stored data for the commodity.
    """

    inputs = cp_output["inputs"]
    forecast_data = cp_output["forecast"]

    summary_text = cp_output.get("summary", "")
    new_summary_text = forecast_data.get("New_Summary", "")

    run_context = inputs["Run_Context"]

    cp_id = run_context["commodity_id"]
    cp_name = run_context["commodity_name"]
    unit_name = run_context["unit_name"]
    month_of_data_received = run_context["month_label"]

    forecast_rows = forecast_data["forecast"]

    now = timezone.now()
    existing_ai_qs = LoadCPAIForecast.objects.filter(
        cp_id=cp_id,
        ai_forecast_flag=True
    )

    existing_base_qs = LoadCPAIForecast.objects.filter(
        cp_id=cp_id,
        base_forecast_flag=True
    )

    if existing_ai_qs.exists():
        for row in forecast_rows:
            timeframe = convert_forecast_month_to_date(
                row["Forecast_dates"]
            )
            if not timeframe:
                continue
            LoadCPAIForecast.objects.filter(
                cp_id=cp_id,
                timeframe=timeframe,
                ai_forecast_flag=True
            ).update(
                forecast_price=str(row["New_Forecast"]),
                da_forecast=str(row["Base_Forecast"]),
                unit_name=unit_name,
                month_of_data_received=month_of_data_received,
                summary=summary_text,
                new_summary=new_summary_text,
                modified_by=created_by,
                modified_at=now
            )
        logger.info("AI rows overwritten for CP: %s", cp_id)
        return len(forecast_rows)

    elif existing_base_qs.exists():
        existing_base_qs.update(
            base_forecast_flag=False,
            is_delete_flag=True,
            modified_by=created_by,
            modified_at=now
        )
        objects_to_create = []
        for row in forecast_rows:
            timeframe = convert_forecast_month_to_date(
                row["Forecast_dates"]
            )
            if not timeframe:
                continue
            obj = LoadCPAIForecast(
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
                modified_at=now
            )
            objects_to_create.append(obj)
        LoadCPAIForecast.objects.bulk_create(objects_to_create)
        logger.info("Base converted → AI inserted for CP: %s", cp_id)
        return len(objects_to_create)

    else:
        objects_to_create = []
        for row in forecast_rows:
            timeframe = convert_forecast_month_to_date(
                row["Forecast_dates"]
            )
            if not timeframe:
                continue
            obj = LoadCPAIForecast(
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
                modified_at=now,
                created_at=now,
                modified_by=created_by,
            )
            objects_to_create.append(obj)
        LoadCPAIForecast.objects.bulk_create(objects_to_create)
        logger.info("Fresh AI rows inserted for CP: %s", cp_id)
        return len(objects_to_create)
