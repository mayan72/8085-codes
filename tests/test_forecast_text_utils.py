import json
import unittest

from helpers.forecast_text_utils import (
    ForecastTextError,
    extract_first_short_term_bullet,
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

    def test_missing_heading_still_errors(self):
        with self.assertRaises(ForecastTextError) as ctx:
            extract_first_short_term_bullet("No outlook headings here.")
        self.assertIn("Short-term Outlook", str(ctx.exception))


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

    def test_trailing_comma_and_smart_quotes(self):
        raw = "{“cp_id”: “JEODN”, “week_of_month”: 3,}"
        payload = parse_json_object(raw)
        self.assertEqual(payload["cp_id"], "JEODN")

    def test_missing_comma_between_objects(self):
        raw = '{"news_dump": [{"id": 1} {"id": 2}]}'
        payload = parse_json_object(raw)
        self.assertEqual(len(payload["news_dump"]), 2)

    def test_unescaped_newline_in_string(self):
        raw = '{\n  "New_Summary": "line1\nline2"\n}'
        payload = parse_json_object(raw)
        self.assertIn("line1", payload["New_Summary"])

    def test_invalid_json_still_raises(self):
        with self.assertRaises(ForecastTextError):
            parse_json_object("not json at all")


if __name__ == "__main__":
    unittest.main()
