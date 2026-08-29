"""Deterministic forecast math from AI_FORECAST_PROMPT sections 2-6.

Language, web search, benchmark choice, price observations, and news
classification stay in the LLM. This module only applies the prompt's
stated formulas to those evidence fields plus USER_INPUTS.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta


ADJUST_CLAMP = 10.0
NEWS_SCORE_SCALE = 1.50
RECENCY_TAU_DAYS = 10.0
NEWS_WINDOW_DAYS = 30
DIRECTION_UP_PP = 0.25
DIRECTION_DOWN_PP = -0.25
CONFLICT_MAGNITUDE_PP = 2.0
CONFLICT_SCALE = 0.70
CONFIDENCE_MAGNITUDE_PP = 6.0
LARGE_TAPER_ABS_PP = 5.0
WEEK_2_3_THRESHOLD_PP = 5.0
WEEK_4_5_THRESHOLD_PP = 2.0
TOP_DRIVERS_MIN = 3
TOP_DRIVERS_MAX = 6

INITIAL_WEIGHTS = {
    1: (0.80, 0.20),
    2: (0.55, 0.45),
    3: (0.45, 0.55),
    4: (0.30, 0.70),
    5: (0.30, 0.70),
}

RISK_FACTOR = {
    "high": 1.00,
    "medium": 0.70,
    "low": 0.40,
}

A_LARGE_MULTIPLIERS = {0: 1.00, 1: 0.75, 2: 0.50, 3: 0.25}


def clamp_adj(value):
    """adj_pct_total / signal clamps from the prompt: [-10.0, +10.0]."""
    if value is None:
        return 0.0
    if value > ADJUST_CLAMP:
        return ADJUST_CLAMP
    if value < -ADJUST_CLAMP:
        return -ADJUST_CLAMP
    return float(value)


def week_of_month(run_date):
    """Section 2: day 01-07=1, 08-14=2, 15-21=3, 22-28=4, 29-end=5."""
    day = run_date.day
    if day <= 7:
        return 1
    if day <= 14:
        return 2
    if day <= 21:
        return 3
    if day <= 28:
        return 4
    return 5


def _parse_run_date(run_date_local):
    return datetime.strptime(str(run_date_local), "%Y-%m-%d").date()


def _parse_iso_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        from dateutil import parser as date_parser

        return date_parser.parse(text).date()
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_month_label(month_label):
    text = str(month_label or "").strip()
    for fmt in ("%b-%Y", "%b-%y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_forecast_dates(forecast_dates):
    text = str(forecast_dates or "").strip()
    try:
        return datetime.strptime(text, "%b-%y")
    except ValueError:
        return _parse_month_label(text)


def _same_month(forecast_dates, month_label):
    left = _parse_forecast_dates(forecast_dates)
    right = _parse_month_label(month_label)
    if left is None or right is None:
        return False
    return left.year == right.year and left.month == right.month


def _mean(values):
    if not values:
        return None
    return float(sum(values)) / float(len(values))


def _as_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dedupe_observations(rows):
    """Keep first of each date/source pair. Prompt: do not duplicate same date/source."""
    seen = set()
    unique = []
    if not isinstance(rows, list):
        return unique
    for item in rows:
        if not isinstance(item, dict):
            continue
        date_text = str(item.get("date") or "").strip()
        url = str(item.get("url") or "").strip()
        price = _as_float(item.get("price"))
        if not date_text or price is None:
            continue
        key = (date_text, url)
        if key in seen:
            continue
        seen.add(key)
        unique.append({"date": date_text, "price": price, "url": url})
    return unique


def _filter_through_run_date(rows, run_date):
    kept = []
    for item in rows:
        parsed = _parse_iso_date(item.get("date"))
        if parsed is None or parsed > run_date:
            continue
        kept.append(item)
    return kept


def initial_weights(week):
    return INITIAL_WEIGHTS[week]


def effective_weights(week, price_available, news_available):
    """Section 2: unavailable signal weight 0, renormalize remaining to 1.0."""
    news_w, avg_w = initial_weights(week)
    if not news_available:
        news_w = 0.0
    if not price_available:
        avg_w = 0.0
    total = news_w + avg_w
    if total <= 0:
        return 0.0, 0.0
    return news_w / total, avg_w / total


def select_base_forecast_current(baseline_rows, month_label):
    """Prefer Forecast_dates matching month_label; else first row (prompt fallback)."""
    if not baseline_rows:
        return None, True
    for row in baseline_rows:
        if _same_month(row.get("Forecast_dates"), month_label):
            return _as_float(row.get("Base_Forecast")), False
    return _as_float(baseline_rows[0].get("Base_Forecast")), True


def compute_price_signal(price_evidence, baseline_rows, run_date, month_label, week):
    """Section 3 numeric rules. Observations and reliability_basis come from the LLM."""
    evidence = price_evidence if isinstance(price_evidence, dict) else {}
    current_obs = _filter_through_run_date(
        _dedupe_observations(evidence.get("daily_or_weekly_prices_current_month")),
        run_date,
    )
    last_obs = _dedupe_observations(evidence.get("daily_or_weekly_prices_last_month"))

    basis = str(evidence.get("reliability_basis") or "").strip().lower()
    if basis not in {"daily", "weekly"}:
        basis = "none"

    n_current = len(current_obs)
    n_last = len(last_obs)
    if basis == "daily":
        reliable = n_current >= 8 and n_last >= 15
    elif basis == "weekly":
        reliable = n_current >= 2 and n_last >= 4
    else:
        reliable = False

    sources = evidence.get("sources") if isinstance(evidence.get("sources"), list) else []
    urls = []
    for item in current_obs + last_obs:
        url = str(item.get("url") or "").strip()
        if url:
            urls.append(url)
    for src in sources:
        if isinstance(src, dict):
            url = str(src.get("url") or "").strip()
            if url:
                urls.append(url)
    unique_urls = {u for u in urls if u}
    single_source = len(unique_urls) == 1

    base_current, used_first_row_fallback = select_base_forecast_current(
        baseline_rows, month_label
    )

    result = {
        "reliable_price_data": bool(reliable),
        "reliability_basis": basis if reliable else ("none" if basis == "none" else basis),
        "sources": sources,
        "single_source": single_source,
        "daily_or_weekly_prices_current_month": current_obs if reliable else current_obs,
        "daily_or_weekly_prices_last_month": last_obs if reliable else last_obs,
        "last_month_avg_price": None,
        "current_month_avg_to_date": None,
        "avg_pct_change_vs_last_month": None,
        "direction_avg": "NA",
        "Base_Forecast_current": base_current,
        "baseline_forecast_pct_change_current": None,
        "delta_pp_vs_baseline": None,
        "threshold_pp_applied": None,
        "avg_adjust_pct_current": 0.0,
        "used_first_row_fallback": used_first_row_fallback,
        "price_available": False,
    }

    if not reliable:
        result["reliability_basis"] = "none"
        result["daily_or_weekly_prices_current_month"] = current_obs
        result["daily_or_weekly_prices_last_month"] = last_obs
        return result

    last_avg = _mean([item["price"] for item in last_obs])
    current_avg = _mean([item["price"] for item in current_obs])
    result["last_month_avg_price"] = last_avg
    result["current_month_avg_to_date"] = current_avg

    if last_avg in (None, 0) or current_avg is None:
        result["reliability_basis"] = "none"
        result["reliable_price_data"] = False
        return result

    avg_pct = ((current_avg / last_avg) - 1.0) * 100.0
    result["avg_pct_change_vs_last_month"] = avg_pct
    if avg_pct > DIRECTION_UP_PP:
        result["direction_avg"] = "Up"
    elif avg_pct < DIRECTION_DOWN_PP:
        result["direction_avg"] = "Down"
    else:
        result["direction_avg"] = "Flat"

    if base_current is None:
        result["reliable_price_data"] = False
        result["reliability_basis"] = "none"
        return result

    baseline_pct = ((base_current / last_avg) - 1.0) * 100.0
    delta_pp = avg_pct - baseline_pct
    result["baseline_forecast_pct_change_current"] = baseline_pct
    result["delta_pp_vs_baseline"] = delta_pp

    if week == 1:
        threshold = None
        avg_adjust = delta_pp
    elif week in (2, 3):
        threshold = WEEK_2_3_THRESHOLD_PP
        avg_adjust = 0.0 if abs(delta_pp) <= threshold else delta_pp
    else:
        threshold = WEEK_4_5_THRESHOLD_PP
        avg_adjust = 0.0 if abs(delta_pp) <= threshold else delta_pp

    result["threshold_pp_applied"] = threshold
    result["avg_adjust_pct_current"] = clamp_adj(avg_adjust)
    result["price_available"] = True
    result["reliability_basis"] = basis
    return result


def _sign_i(price_impact):
    text = str(price_impact or "").strip().lower()
    if text == "up":
        return 1
    if text == "down":
        return -1
    return 0


def _article_magnitude(pos_rating, neg_rating, sign_i):
    """
    Prompt: net_rating = pos - neg.
    If both impact ratings are 0 and sign_i != 0, magnitude = 1.
    Otherwise magnitude is |net_rating| so sign_i carries direction.
    """
    pos = 0 if pos_rating is None else pos_rating
    neg = 0 if neg_rating is None else neg_rating
    net_rating = pos - neg
    if pos == 0 and neg == 0 and sign_i != 0:
        return 1.0, net_rating
    return float(abs(net_rating)), net_rating


def _article_score(article, run_date):
    sign = _sign_i(article.get("price_impact"))
    pos = article.get("pos_impact_rating")
    neg = article.get("neg_impact_rating")
    try:
        pos_i = int(pos) if pos is not None else 0
        neg_i = int(neg) if neg is not None else 0
    except (TypeError, ValueError):
        pos_i, neg_i = 0, 0

    magnitude, net_rating = _article_magnitude(pos_i, neg_i, sign)
    risk_key = str(article.get("risk_rating") or "").strip().lower()
    risk_factor = RISK_FACTOR.get(risk_key)
    if risk_factor is None:
        return 0.0, sign, net_rating

    article_date = _parse_iso_date(article.get("date"))
    if article_date is None:
        return 0.0, sign, net_rating
    days_since = (run_date - article_date).days
    recency_factor = math.exp(-(float(days_since)) / RECENCY_TAU_DAYS)

    try:
        is_relevant = int(article.get("is_relevant") or 0)
    except (TypeError, ValueError):
        is_relevant = 0
    relevance_factor = 1.00 if is_relevant == 1 else 0.50

    score = sign * magnitude * risk_factor * recency_factor * relevance_factor
    return float(score), sign, net_rating


def compute_news_signal(news_dump, run_date):
    """Section 4 scoring. Article labels come from the LLM; scores are Python."""
    articles = news_dump if isinstance(news_dump, list) else []
    usable = [item for item in articles if isinstance(item, dict)]
    window_start = run_date - timedelta(days=NEWS_WINDOW_DAYS)
    scored = []
    for item in usable:
        article_date = _parse_iso_date(item.get("date"))
        if article_date is None or article_date < window_start or article_date > run_date:
            continue
        score, sign, net_rating = _article_score(item, run_date)
        scored.append((item, score, sign, net_rating))

    news_available = len(scored) > 0
    net_news_score = sum(item[1] for item in scored) if news_available else 0.0
    news_adjust = clamp_adj(NEWS_SCORE_SCALE * net_news_score) if news_available else 0.0

    ranked = sorted(scored, key=lambda row: abs(row[1]), reverse=True)
    driver_count = min(TOP_DRIVERS_MAX, len(ranked))

    top_drivers = []
    for index, (item, score, sign, net_rating) in enumerate(ranked[:driver_count], start=1):
        impact = str(item.get("price_impact") or "Neutral")
        if impact not in {"Up", "Down", "Neutral", "Unknown"}:
            if sign > 0:
                impact = "Up"
            elif sign < 0:
                impact = "Down"
            else:
                impact = "Neutral"
        why = item.get("why_it_matters") or item.get("evidence_quote") or item.get("title") or ""
        top_drivers.append(
            {
                "id": item.get("id") if item.get("id") is not None else index,
                "sign": impact if impact != "Unknown" else "Neutral",
                "why_it_matters": str(why),
            }
        )

    return {
        "news_available": news_available,
        "news_count_total": len(usable),
        "news_count_used": len(scored),
        "net_news_score": float(net_news_score) if news_available else 0.0,
        "news_adjust_pct_current": float(news_adjust) if news_available else 0.0,
        "top_drivers": top_drivers,
        "news_dump": usable,
    }


def combine_signals(news_w, avg_w, news_adjust, avg_adjust, news_available, price_available):
    """Section 5."""
    if not news_available and not price_available:
        return {
            "total_adjust_pct_current": 0.0,
            "confidence": "High",
            "conflict_flag": False,
        }

    news_term = news_adjust if news_available else 0.0
    avg_term = avg_adjust if price_available else 0.0
    total = news_w * news_term + avg_w * avg_term

    both_available = news_available and price_available
    opposite = (news_term > 0 and avg_term < 0) or (news_term < 0 and avg_term > 0)
    both_large = abs(news_term) >= CONFLICT_MAGNITUDE_PP and abs(avg_term) >= CONFLICT_MAGNITUDE_PP
    if both_available and opposite and both_large:
        total = CONFLICT_SCALE * total
        conflict = True
        confidence = "Low"
    else:
        conflict = False
        mag_news = abs(news_term) if news_available else 0.0
        mag_avg = abs(avg_term) if price_available else 0.0
        if mag_news >= CONFIDENCE_MAGNITUDE_PP or mag_avg >= CONFIDENCE_MAGNITUDE_PP:
            confidence = "Medium"
        else:
            confidence = "High"

    return {
        "total_adjust_pct_current": clamp_adj(total),
        "confidence": confidence,
        "conflict_flag": conflict,
    }


def taper_multiplier(h, n, adj_pct_base):
    """Section 6. h is zero-based row index (validator requires horizon_index == row order)."""
    if abs(adj_pct_base) > LARGE_TAPER_ABS_PP:
        if h >= 4:
            return 0.00, "A_large"
        return A_LARGE_MULTIPLIERS[h], "A_large"
    if n == 1:
        return 1.0, "B_small"
    if n <= 1:
        return 1.0, "B_small"
    return max(0.0, 1.0 - (float(h) / float(n - 1))), "B_small"


def build_forecast_rows(baseline_rows, adj_pct_base):
    n = len(baseline_rows)
    rows = []
    multipliers = []
    taper_case = "B_small"
    for h, src in enumerate(baseline_rows):
        base = float(src["Base_Forecast"])
        mult, taper_case = taper_multiplier(h, n, adj_pct_base)
        adj_total = clamp_adj(adj_pct_base * mult)
        new_forecast = base * (1.0 + adj_total / 100.0)
        rows.append(
            {
                "Forecast_dates": src["Forecast_dates"],
                "Base_Forecast": base,
                "New_Forecast": new_forecast,
                "horizon_index": h,
                "adj_pct_total": adj_total,
                "taper_multiplier": mult,
            }
        )
        multipliers.append({"horizon_index": h, "multiplier": mult})
    return rows, multipliers, taper_case


def _new_summary(week, news_w, avg_w, price, news, combined, taper_case, n_rows):
    """Hyphen-led calculation description. Not LLM prose."""
    lines = [
        f"- Week-of-month {week} with effective news weight {news_w:.2f} and avg-to-date weight {avg_w:.2f}.",
        f"- Price signal reliable={price.get('reliable_price_data')} "
        f"avg_adjust_pct_current={price.get('avg_adjust_pct_current')}.",
        f"- News signal news_count_used={news.get('news_count_used')} "
        f"net_news_score={news.get('net_news_score')} "
        f"news_adjust_pct_current={news.get('news_adjust_pct_current')}.",
        f"- Combined total_adjust_pct_current={combined.get('total_adjust_pct_current')} "
        f"confidence={combined.get('confidence')} conflict_flag={combined.get('conflict_flag')}.",
        f"- Taper case {taper_case} applied across {n_rows} forecast rows with +/-{ADJUST_CLAMP:g}% cap.",
    ]
    for driver in (news.get("top_drivers") or [])[:3]:
        why = str(driver.get("why_it_matters") or "").strip()
        if why:
            lines.append(f"- Driver {driver.get('id')} ({driver.get('sign')}): {why}")
        if len(lines) >= 8:
            break
    return "\n".join(lines[:8])


def _calculation_notes(week, price, llm_notes):
    notes = [
        f"week_of_month={week}.",
        f"reliability_basis={price.get('reliability_basis')} "
        f"reliable_price_data={price.get('reliable_price_data')}.",
    ]
    if price.get("used_first_row_fallback"):
        notes.append(
            "No exact current-month Baseline_Forecast_Rows match for month_label; "
            "used the first baseline row for Base_Forecast_current."
        )
    extra = str(llm_notes or "").strip()
    if extra:
        notes.append(extra)
    return " ".join(notes)


def assemble_forecast_output(user_inputs, llm_evidence):
    """
    Same USER_INPUTS as the prompt plus LLM evidence JSON.
    Returns the existing forecast output contract for validate_forecast_output.
    """
    run_context = user_inputs["Run_Context"]
    baseline_rows = list(user_inputs["Baseline_Forecast_Rows"])
    evidence = llm_evidence if isinstance(llm_evidence, dict) else {}
    run_date = _parse_run_date(run_context["run_date_local"])
    week = week_of_month(run_date)

    price = compute_price_signal(
        evidence.get("price_data") or {},
        baseline_rows,
        run_date,
        run_context.get("month_label"),
        week,
    )
    news = compute_news_signal(evidence.get("news_dump") or [], run_date)

    news_w, avg_w = effective_weights(
        week, price["price_available"], news["news_available"]
    )
    both_unavailable = (news_w == 0.0 and avg_w == 0.0)

    if both_unavailable:
        combined = {
            "total_adjust_pct_current": 0.0,
            "confidence": "High",
            "conflict_flag": False,
        }
        adj_base = 0.0
    else:
        combined = combine_signals(
            news_w,
            avg_w,
            news["news_adjust_pct_current"],
            price["avg_adjust_pct_current"],
            news["news_available"],
            price["price_available"],
        )
        adj_base = combined["total_adjust_pct_current"]

    forecast_rows, multipliers, taper_case = build_forecast_rows(baseline_rows, adj_base)

    price_out = {key: value for key, value in price.items() if key not in {"price_available", "used_first_row_fallback"}}

    benchmark = evidence.get("benchmark_selected") if isinstance(evidence.get("benchmark_selected"), dict) else {}
    benchmark_selected = {
        "name": benchmark.get("name") or "",
        "region": benchmark.get("region") or run_context.get("region") or "",
        "currency": benchmark.get("currency") or "",
        "uom": benchmark.get("uom") or run_context.get("unit_name") or "",
        "notes": benchmark.get("notes") or "",
    }

    return {
        "cp_id": str(run_context["commodity_id"]),
        "cp_name": str(run_context["commodity_name"]),
        "unit_name": str(run_context["unit_name"]),
        "run_date_local": str(run_context["run_date_local"]),
        "month_label": str(run_context["month_label"]),
        "benchmark_selected": benchmark_selected,
        "week_of_month": week,
        "weights": {"news": news_w, "avg_to_date": avg_w},
        "price_data": price_out,
        "news_dump": news["news_dump"],
        "news_inputs_used": {
            "news_count_total": news["news_count_total"],
            "news_count_used": news["news_count_used"],
            "net_news_score": news["net_news_score"],
            "news_adjust_pct_current": news["news_adjust_pct_current"],
            "top_drivers": news["top_drivers"],
        },
        "combined_signal": combined,
        "tapering": {
            "adj_pct_base": adj_base,
            "taper_case": taper_case,
            "taper_multipliers": multipliers,
        },
        "forecast": forecast_rows,
        "New_Summary": _new_summary(
            week, news_w, avg_w, price, news, combined, taper_case, len(forecast_rows)
        ),
        "Calculation_Notes": _calculation_notes(
            week, price, evidence.get("Calculation_Notes")
        ),
    }
