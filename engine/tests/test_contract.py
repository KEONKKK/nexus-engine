"""Contract test: design/contracts/lead-fixtures.json vs the Python engine.

Window A owns the fixture file (canonical source per category -- "js" for most,
"python" for parse). This file checks exactly what round 3 decided to check:

  * phone   -- classification agreement (ok / not-ok), with two DOCUMENTED,
               deliberately unfixed leniency gaps (KNOWN_PHONE_GAPS below).
  * wa_link -- given the finished message text JS already built, does
               whatsapp.wa_link() produce the identical wa.me URL?
  * parse   -- Python is canonical here, so there is nothing from JS to
               compare against. A frozen regression baseline
               (fixtures/parse-expected.json), generated once from
               scoring.parse_text, is watched for silent drift instead.

score and kits are declared "parity": false in the fixture file itself --
design/reviews/2026-11-08-engine-and-lib.md showed they are two different
products under one shared name, and round 3 decided not to align them.
Aligning them is a product decision for a later round, not a bug fix, so
this file only guards that the fixture still records that decision; it does
not test score or kits behavior.
"""
import json
import unittest
from pathlib import Path

from nexus_master import guardian, scoring, whatsapp

FIXTURES_PATH = Path(__file__).resolve().parents[2] / "design" / "contracts" / "lead-fixtures.json"
PARSE_EXPECTED_PATH = Path(__file__).resolve().parent / "fixtures" / "parse-expected.json"

# Not fixed this round on purpose -- see design/reviews/2026-11-08-engine-and-lib.md,
# section 3. Each key is the exact fixture "in" value that disagrees with JS.
KNOWN_PHONE_GAPS = {
    "055@9814818": "normalize_phone strips embedded punctuation instead of rejecting it "
                    "(phone.mjs calls this bad_characters)",
    "0123456789": "normalize_phone does not validate the Israeli local-part prefix "
                  "(phone.mjs calls this bad_prefix)",
}


def _load_fixtures():
    with open(FIXTURES_PATH, encoding="utf-8") as f:
        return json.load(f)


class TestPhoneContract(unittest.TestCase):
    def test_classification_agrees_except_documented_gaps(self):
        fx = _load_fixtures()
        self.assertEqual(fx["phone"]["canonical"], "js")
        unexpected = []
        for item in fx["phone"]["items"]:
            raw = item["in"]
            js_ok = item["out"]["ok"]
            py_ok = guardian.normalize_phone(raw) is not None
            if py_ok != js_ok and raw not in KNOWN_PHONE_GAPS:
                unexpected.append({"in": raw, "js_ok": js_ok, "py_ok": py_ok})
        self.assertEqual(unexpected, [], f"new, undocumented phone gap(s): {unexpected}")

    def test_known_gaps_have_not_silently_changed_shape(self):
        # If a key here starts agreeing with JS, the gap is gone -- good news,
        # but KNOWN_PHONE_GAPS above should shrink to match, loudly, not quietly.
        fx = _load_fixtures()
        by_in = {item["in"]: item for item in fx["phone"]["items"] if isinstance(item["in"], str)}
        still_gaps = set()
        for raw in KNOWN_PHONE_GAPS:
            item = by_in[raw]
            py_ok = guardian.normalize_phone(raw) is not None
            if py_ok != item["out"]["ok"]:
                still_gaps.add(raw)
        self.assertEqual(still_gaps, set(KNOWN_PHONE_GAPS))

    def test_agreeing_items_match_e164_digits(self):
        fx = _load_fixtures()
        for item in fx["phone"]["items"]:
            raw = item["in"]
            if raw in KNOWN_PHONE_GAPS or not item["out"]["ok"]:
                continue
            digits = guardian.normalize_phone(raw)
            self.assertEqual("+" + digits, item["out"]["e164"], f"in={raw!r}")


class TestWaLinkContract(unittest.TestCase):
    def test_python_wa_link_matches_js_url_for_the_finished_text(self):
        fx = _load_fixtures()
        self.assertEqual(fx["wa_link"]["canonical"], "js")
        for i, item in enumerate(fx["wa_link"]["items"]):
            text = item["out"]["text"]
            url = whatsapp.wa_link(guardian.DEFAULT_BUSINESS_NUMBER, text)
            self.assertEqual(url, item["out"]["url"], f"item {i}")


class TestScoreAndKitsAreDeclaredNonParity(unittest.TestCase):
    """Guards that the fixture still records the "two different products"
    decision -- does not test score or kits behavior itself."""

    def test_score_declared_non_parity(self):
        fx = _load_fixtures()
        self.assertFalse(fx["score"].get("parity", True))

    def test_kits_declared_non_parity(self):
        fx = _load_fixtures()
        self.assertFalse(fx["kits"].get("parity", True))


class TestParseRegression(unittest.TestCase):
    """parse is Python-canonical: fixtures/parse-expected.json is a frozen
    snapshot of scoring.parse_text's own output, not a comparison to JS.
    A red here means parse_text's behavior drifted, intentionally or not."""

    def test_parse_matches_frozen_baseline(self):
        fx = _load_fixtures()
        with open(PARSE_EXPECTED_PATH, encoding="utf-8") as f:
            expected = json.load(f)
        expected_by_id = {e["id"]: e["out"] for e in expected["items"]}
        self.assertEqual(set(expected_by_id), {it["id"] for it in fx["parse"]["items"]})
        for item in fx["parse"]["items"]:
            got = scoring.parse_text(item["text"])
            self.assertEqual(got, expected_by_id[item["id"]], f"id={item['id']}")


if __name__ == "__main__":
    unittest.main()
