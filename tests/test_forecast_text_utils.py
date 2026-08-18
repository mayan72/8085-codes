"""Unit tests for first-bullet extraction used by the AI summary prompt."""

import io
import logging
import unittest

from helpers.forecast_text_utils import (
    ForecastTextError,
    build_ai_summary_prompt,
    extract_first_omega_bullet,
    extract_first_short_term_bullet,
    get_first_short_term_bullet_for_prompt,
    parse_json_object,
)


class ExtractShortTermOutlookTests(unittest.TestCase):
    def test_plain_heading_own_line(self):
        analysis = (
            "Commodity: HDPE\n"
            "Short-term Outlook\n"
            "- Prices are expected to rise on tight supply.\n"
            "Long-term Outlook\n"
            "- Capacity additions weigh on prices.\n"
        )
        bullet, start, end = extract_first_short_term_bullet(analysis)
        self.assertIn("rise on tight supply", bullet)
        self.assertTrue(analysis[start:end])

    def test_heading_with_colon_and_same_line_body(self):
        analysis = (
            "Short-term Outlook: Prices are expected to fall in August.\n"
            "Long-term Outlook\n"
            "Further weakness is likely.\n"
        )
        bullet, _, _ = extract_first_short_term_bullet(analysis)
        self.assertEqual(bullet, "Prices are expected to fall in August.")

    def test_markdown_and_unhyphenated_heading(self):
        analysis = (
            "**Short Term Outlook**\n"
            "Prices are expected to remain stable.\n\n"
            "**Long-term Outlook**\n"
            "Balanced market.\n"
        )
        bullet, _, _ = extract_first_short_term_bullet(analysis)
        self.assertIn("remain stable", bullet)

    def test_markdown_hashes(self):
        analysis = (
            "### Short-term Outlook\n"
            "• Demand recovers into Q4.\n"
            "### Long-term Outlook\n"
            "• Oversupply remains.\n"
        )
        bullet, _, _ = extract_first_short_term_bullet(analysis)
        self.assertIn("Demand recovers", bullet)

    def test_omega_first_bullet_keeps_nested_pi_sigma(self):
        analysis = (
            "Commodity: PVC\n"
            "Region: Global\n"
            "Short-term Outlook\n"
            "Ω Prices are expected to rise in Aug 2026 on tight supply.\n"
            "π Spot availability remains constrained.\n"
            "Σ Plant turnarounds limit output.\n"
            "Ω Prices are expected to fall in Sep 2026 on new capacity.\n"
            "Long-term Outlook\n"
            "Ω Capacity additions weigh on prices.\n"
        )
        bullet, _, _ = extract_first_short_term_bullet(analysis)
        self.assertIn("rise in Aug 2026", bullet)
        self.assertIn("Spot availability remains constrained", bullet)
        self.assertIn("Plant turnarounds", bullet)
        self.assertNotIn("fall in Sep 2026", bullet)
        self.assertNotIn("Capacity additions", bullet)

    def test_missing_heading_still_errors(self):
        with self.assertRaises(ForecastTextError) as ctx:
            extract_first_short_term_bullet("No outlook headings here.")
        self.assertIn("Short-term Outlook", str(ctx.exception))


class OmegaFallbackTests(unittest.TestCase):
    def test_first_omega_when_heading_missing(self):
        analysis = (
            "Commodity: HDPE\n"
            "Ω Prices are expected to rise on restocking.\n"
            "π Convertor demand is firm.\n"
            "Ω Longer-term oversupply remains.\n"
        )
        bullet, _, _ = extract_first_omega_bullet(analysis)
        self.assertIn("rise on restocking", bullet)
        self.assertIn("Convertor demand is firm", bullet)
        self.assertNotIn("oversupply remains", bullet)


class FirstBulletForPromptTests(unittest.TestCase):
    def _capture_logger(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        test_logger = logging.getLogger("test.first_bullet")
        test_logger.handlers = [handler]
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False
        return test_logger, stream

    def test_uses_short_term_outlook_and_logs(self):
        analysis = (
            "Short-term Outlook\n"
            "Ω Prices are expected to rise in Aug 2026.\n"
            "Long-term Outlook\n"
            "Ω Balanced market.\n"
        )
        test_logger, stream = self._capture_logger()
        bullet = get_first_short_term_bullet_for_prompt(
            analysis, log=test_logger, cp_id="01IPN"
        )
        logs = stream.getvalue()
        self.assertEqual(bullet, "Ω Prices are expected to rise in Aug 2026.")
        self.assertIn("source=short_term_outlook", logs)
        self.assertIn("cp_id=01IPN", logs)
        self.assertIn("[FIRST BULLET TEXT]", logs)

    def test_falls_back_to_omega_and_logs_warning(self):
        analysis = "Ω Prices are expected to fall on weaker demand.\n"
        test_logger, stream = self._capture_logger()
        bullet = get_first_short_term_bullet_for_prompt(
            analysis, log=test_logger, cp_id="L4S1M"
        )
        logs = stream.getvalue()
        self.assertIn("fall on weaker demand", bullet)
        self.assertIn("short_term_outlook failed", logs)
        self.assertIn("source=omega_fallback", logs)

    def test_empty_analysis_returns_empty_string(self):
        test_logger, stream = self._capture_logger()
        bullet = get_first_short_term_bullet_for_prompt(
            "", log=test_logger, cp_id="JEODN"
        )
        self.assertEqual(bullet, "")
        self.assertIn("source=empty", stream.getvalue())

    def test_prompt_placeholder_is_replaced(self):
        analysis = (
            "Short-term Outlook\n"
            "Ω Prices are expected to rise in Aug 2026.\n"
            "Long-term Outlook\n"
            "Ω Balanced market.\n"
        )
        bullet = get_first_short_term_bullet_for_prompt(analysis, cp_id="01IPN")
        prompt = (
            "ORIGINAL_FIRST_SHORT_TERM_BULLET:\n"
            "<<ORIGINAL_FIRST_SHORT_TERM_BULLET>>\n"
        )
        filled = prompt.replace("<<ORIGINAL_FIRST_SHORT_TERM_BULLET>>", bullet)
        self.assertIn("Ω Prices are expected to rise in Aug 2026.", filled)
        self.assertNotIn("<<ORIGINAL_FIRST_SHORT_TERM_BULLET>>", filled)


class BuildSummaryPromptTests(unittest.TestCase):
    def test_fills_first_bullet_and_forecast_revision_placeholders(self):
        template = (
            "Commodity: <<COMMODITY_NAME>>\n"
            "Region: <<REGION>>\n"
            "News:\n<<NEWS_ARTICLES>>\n"
            "Base:\n<<BASE_ANALYSIS>>\n"
            "Last: <<LAST_ACTUAL>>\n"
            "Revised from <<PREVIOUS_FORECAST>> to <<FORECAST_PRICE>>\n"
            "ORIGINAL_FIRST_SHORT_TERM_BULLET:\n"
            "<<ORIGINAL_FIRST_SHORT_TERM_BULLET>>\n"
        )
        analysis = (
            "Short-term Outlook\n"
            "Ω Prices are expected to rise in Aug 2026.\n"
            "Long-term Outlook\n"
            "Ω Balanced market.\n"
        )
        first_bullet = get_first_short_term_bullet_for_prompt(analysis, cp_id="01IPN")
        filled = build_ai_summary_prompt(
            template,
            news_articles="Plant outage in Asia.",
            base_articles=analysis,
            commodity_name="HDPE",
            region="Global",
            last_actual=980,
            previous_forecast=1000,
            revised_forecast=1025,
            first_short_term_bullet=first_bullet,
            cp_id="01IPN",
        )
        self.assertIn("Ω Prices are expected to rise in Aug 2026.", filled)
        self.assertIn("Revised from 1000 to 1025", filled)
        self.assertNotIn("<<ORIGINAL_FIRST_SHORT_TERM_BULLET>>", filled)
        self.assertNotIn("<<PREVIOUS_FORECAST>>", filled)


class ParseJsonObjectTests(unittest.TestCase):
    def test_plain_object(self):
        payload = parse_json_object('{"cp_id": "JEODN", "week_of_month": 3}')
        self.assertEqual(payload["cp_id"], "JEODN")

    def test_fenced_and_prose_wrapper(self):
        raw = (
            "Here is the result:\n"
            "```json\n"
            '{"cp_id": "JEODN", "forecast": []}\n'
            "```\n"
            "thanks"
        )
        payload = parse_json_object(raw)
        self.assertEqual(payload["forecast"], [])


if __name__ == "__main__":
    unittest.main()
