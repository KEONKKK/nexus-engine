"""Tests for public.public_api.App -- direct WSGI calls, no socket, no server (layer 1 of
round-3-proxy-spec.md's two-layer test strategy). Covers all 15 requests in
design/corpus/proxy-requests.json by id (p-01..p-15), plus round 4's notify_link and
/public/partners additions. Adopted from design/plans/proxy-draft/test_public_api.py
(window C, 20/20 green) and extended by B when adopting into engine/.

Run via: npm run test:engine  (python -m unittest discover -s engine/tests -t engine)
"""
import io
import json
import unittest

from nexus_master import db

from public.public_api import App


class Base(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.app = App(self.c)

    def call(self, method, path, body=None, remote="127.0.0.1", raw_body=None):
        if raw_body is not None:
            raw = raw_body.encode("utf-8")
        else:
            raw = json.dumps(body).encode("utf-8") if body is not None else b""
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
               "REMOTE_ADDR": remote, "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
        st = {}
        out = self.app(env, lambda s, h: st.setdefault("s", s))
        return int(st["s"].split()[0]), json.loads(b"".join(out))


# ---- valid requests (p-01..p-06) --------------------------------------

class TestValidLeads(Base):
    def test_p01_valid_pro_lead(self):
        s, d = self.call("POST", "/public/leads", {
            "category": "pro", "text": "מחפש אינסטלטור דחוף, נזילה במטבח", "name": "דנה כהן",
            "phone": "+972501234567", "city": "הרצליה", "trade": "אינסטלטור", "urgency": "today",
            "consent": True})
        self.assertEqual(s, 201)
        self.assertTrue(d["ok"])
        self.assertEqual(d["status"], "new")
        self.assertTrue(d["tracking_token"])

    def test_p02_valid_realestate_lead(self):
        s, d = self.call("POST", "/public/leads", {
            "category": "realestate", "text": "מחפשים דירת 4 חדרים לקנייה באזור המרכז",
            "name": "יוסי לוי", "phone": "+972521112233", "city": "רעננה", "deal_type": "buy",
            "property_type": "apartment", "budget_max": 2500000, "timeline": "3-6m", "consent": True})
        self.assertEqual(s, 201)
        self.assertTrue(d["ok"])

    def test_p03_valid_kit_category_lead(self):
        s, d = self.call("POST", "/public/leads", {
            "category": "kit", "text": "בעל עסק קטן, רוצה תסריטי שיחה ל-WhatsApp",
            "name": "מאיה אברהם", "phone": "+972541234567", "city": "חיפה",
            "business_type": "beauty", "pain": "אין לי זמן לכתוב הודעות", "business_size": "solo",
            "kit_requested": True, "consent": True})
        self.assertEqual(s, 201)
        self.assertTrue(d["ok"])


class TestValidKitsAndStatusAndHealth(Base):
    def test_p04_valid_kit_request(self):
        s, d = self.call("POST", "/public/kits",
                         {"kind": "whatsapp", "business": "מאיה קוסמטיקה", "industry": "beauty"})
        self.assertEqual(s, 201)
        self.assertTrue(d["ok"])

    def test_p05_valid_status_check(self):
        _, created = self.call("POST", "/public/leads", {
            "category": "pro", "text": "מחפש חשמלאי", "phone": "+972500000099",
            "trade": "חשמלאי", "consent": True})
        token = created["tracking_token"]
        s, d = self.call("GET", f"/public/leads/{token}/status")
        self.assertEqual(s, 200)
        self.assertEqual(d["status"], "new")
        # Gross on purpose -- no score, phone, text or notify_link leaks through.
        self.assertNotIn("score", d)
        self.assertNotIn("phone", d)
        self.assertNotIn("text", d)
        self.assertNotIn("notify_link", d)

    def test_p06_valid_health(self):
        s, d = self.call("GET", "/public/health")
        self.assertEqual(s, 200)
        self.assertEqual(d, {"ok": True})


# ---- missing_field (p-07, p-08) ----------------------------------------

class TestMissingField(Base):
    def test_p07_missing_category(self):
        s, d = self.call("POST", "/public/leads", {
            "text": "מחפש חשמלאי", "name": "אבי שרון", "phone": "+972501112222", "consent": True})
        self.assertEqual(s, 400)
        self.assertFalse(d["ok"])
        self.assertIn("category", [e["field"] for e in d["errors"]])

    def test_p08_missing_kind(self):
        s, d = self.call("POST", "/public/kits", {"business": "קבוצת פיט", "industry": "fitness"})
        self.assertEqual(s, 400)
        self.assertFalse(d["ok"])
        self.assertIn("kind", [e["field"] for e in d["errors"]])


# ---- fake_phone (p-09) ---------------------------------------------------

class TestFakePhone(Base):
    def test_p09_fake_phone_rejected(self):
        s, d = self.call("POST", "/public/leads", {
            "category": "pro", "text": "צריך מנעולן, נעול בחוץ", "name": "רונית בר",
            "phone": "123", "city": "לוד", "trade": "מנעולן", "urgency": "now", "consent": True})
        self.assertEqual(s, 400)
        errors = {e["field"]: e["reason"] for e in d["errors"]}
        self.assertEqual(errors.get("phone"), "bad_format")


# ---- no_consent (p-10) ---------------------------------------------------

class TestNoConsent(Base):
    def test_p10_missing_consent_rejected(self):
        s, d = self.call("POST", "/public/leads", {
            "category": "pro", "text": "מחפש גנן לגינה קטנה", "name": "עומר כץ",
            "phone": "+972509998877", "city": "מודיעין", "trade": "גנן", "urgency": "flexible"})
        self.assertEqual(s, 400)
        errors = {e["field"]: e["reason"] for e in d["errors"]}
        self.assertEqual(errors.get("consent"), "required")


# ---- rate_limit (p-11, p-12) ---------------------------------------------

class TestRateLimit(Base):
    def test_p11_leads_rate_limit_5_per_minute(self):
        body = {"category": "pro", "text": "בדיקת rate-limit, ליד תקין לכל הפרטים",
                "name": "בדיקה", "phone": "+972500000001", "city": "אשדוד", "trade": "חשמלאי",
                "urgency": "today", "consent": True}
        statuses = [self.call("POST", "/public/leads", body)[0] for _ in range(6)]
        self.assertEqual(statuses[:5], [201] * 5)
        self.assertEqual(statuses[5], 429)

    def test_p12_status_rate_limit_20_per_minute(self):
        _, created = self.call("POST", "/public/leads", {
            "category": "pro", "text": "מחפש שיפוצניק", "phone": "+972500000002",
            "trade": "שיפוצים", "consent": True})
        token = created["tracking_token"]
        statuses = [self.call("GET", f"/public/leads/{token}/status")[0] for _ in range(21)]
        self.assertEqual(statuses[:20], [200] * 20)
        self.assertEqual(statuses[20], 429)


# ---- body_too_large (p-13) -----------------------------------------------

class TestBodyTooLarge(Base):
    def test_p13_body_over_1mb_rejected_with_413(self):
        big_text = "x" * 1_100_000
        body = {"category": "pro", "text": big_text, "phone": "+972500000003",
               "trade": "חשמלאי", "consent": True}
        s, d = self.call("POST", "/public/leads", body)
        self.assertEqual(s, 413)
        self.assertFalse(d["ok"])


# ---- broken_json (p-14) --------------------------------------------------

class TestBrokenJson(Base):
    def test_p14_broken_json_rejected(self):
        s, d = self.call("POST", "/public/leads",
                         raw_body='{"category": "pro", "text": "מחפש מנקה בית", "phone": ')
        self.assertEqual(s, 400)
        self.assertEqual(d.get("error"), "invalid_json")


# ---- unknown_token (p-15) ------------------------------------------------

class TestUnknownToken(Base):
    def test_p15_unknown_token_404(self):
        s, d = self.call("GET", "/public/leads/does-not-exist-00000000/status")
        self.assertEqual(s, 404)
        self.assertFalse(d["ok"])


# ---- a few extra guards worth having, beyond the 15 fixtures ------------

class TestExtras(Base):
    def test_health_is_exempt_from_rate_limit(self):
        statuses = [self.call("GET", "/public/health")[0] for _ in range(30)]
        self.assertTrue(all(s == 200 for s in statuses))

    def test_duplicate_lead_reports_duplicate_status(self):
        body = {"category": "pro", "text": "מחפש אינסטלטור", "phone": "+972501234999",
               "trade": "אינסטלטור", "consent": True}
        self.call("POST", "/public/leads", body)
        s, d = self.call("POST", "/public/leads", dict(body, text="מחפש אינסטלטור שוב"))
        self.assertEqual(s, 201)
        self.assertEqual(d["status"], "duplicate")

    def test_stop_message_returns_200_opted_out_not_an_error(self):
        s, d = self.call("POST", "/public/leads", {
            "category": "pro", "text": "הסר", "phone": "+972501234988", "trade": "חשמלאי",
            "consent": True})
        self.assertEqual(s, 200)
        self.assertTrue(d["opted_out"])

    def test_kit_with_unknown_tracking_token_rejected(self):
        s, d = self.call("POST", "/public/kits",
                         {"tracking_token": "no-such-token", "kind": "cv", "business": "x"})
        self.assertEqual(s, 400)
        self.assertEqual(d["errors"][0]["field"], "tracking_token")

    def test_kit_links_to_lead_via_valid_tracking_token(self):
        _, created = self.call("POST", "/public/leads", {
            "category": "kit", "text": "רוצה ערכה", "phone": "+972501234977",
            "business_type": "beauty", "consent": True})
        token = created["tracking_token"]
        s, d = self.call("POST", "/public/kits",
                         {"tracking_token": token, "kind": "whatsapp", "business": "x", "industry": "beauty"})
        self.assertEqual(s, 201)


# ---- round 4: notify_link on every successful lead ------------------------

class TestLeadNotifyLink(Base):
    def test_successful_lead_gets_a_notify_link_saved(self):
        from nexus_master import leads as leads_module
        s, created = self.call("POST", "/public/leads", {
            "category": "pro", "text": "מחפש שיפוצניק דחוף", "phone": "+972501230001",
            "trade": "שיפוצים", "urgency": "today", "consent": True})
        self.assertEqual(s, 201)
        lid = self.app._resolve_token(created["tracking_token"])
        lead = leads_module.get(self.c, self.app.tid, lid)
        self.assertTrue(lead["notify_link"].startswith("https://wa.me/972559814818"))
        self.assertEqual(lead["notify_clicked"], 0)

    def test_opted_out_message_gets_no_notify_link(self):
        s, d = self.call("POST", "/public/leads", {
            "category": "pro", "text": "הסר", "phone": "+972501230002", "trade": "חשמלאי",
            "consent": True})
        self.assertEqual(s, 200)
        self.assertTrue(d["opted_out"])  # nothing to look up -- no lead row was created


# ---- round 4: POST /public/partners ----------------------------------------

class TestPublicPartners(Base):
    def test_valid_supplier_registration_is_pending(self):
        s, d = self.call("POST", "/public/partners", {
            "name": "דני אינסטלציה", "phone": "+972501230003", "services": "אינסטלטור",
            "areas": "חולון", "type": "supplier", "consent": True})
        self.assertEqual(s, 201)
        self.assertTrue(d["ok"])
        self.assertEqual(d["status"], "pending")
        self.assertTrue(d["partner_id"])

    def test_valid_buyer_registration_is_pending(self):
        s, d = self.call("POST", "/public/partners", {
            "name": "קבוצת נדל״ן", "phone": "+972501230004", "services": "realestate",
            "areas": "תל אביב", "type": "buyer", "consent": True})
        self.assertEqual(s, 201)
        self.assertEqual(d["status"], "pending")

    def test_missing_type_rejected(self):
        s, d = self.call("POST", "/public/partners", {
            "name": "x", "phone": "+972501230005", "consent": True})
        self.assertEqual(s, 400)
        self.assertIn("type", [e["field"] for e in d["errors"]])

    def test_bad_phone_rejected(self):
        s, d = self.call("POST", "/public/partners", {
            "name": "x", "phone": "123", "type": "supplier", "consent": True})
        self.assertEqual(s, 400)
        errors = {e["field"]: e["reason"] for e in d["errors"]}
        self.assertEqual(errors.get("phone"), "bad_format")

    def test_no_consent_rejected(self):
        s, d = self.call("POST", "/public/partners", {
            "name": "x", "phone": "+972501230006", "type": "supplier"})
        self.assertEqual(s, 400)
        self.assertIn("consent", [e["field"] for e in d["errors"]])

    def test_partner_rate_limited_5_per_minute(self):
        body = {"name": "x", "phone": "+972501230007", "type": "supplier", "consent": True}
        statuses = [self.call("POST", "/public/partners", body)[0] for _ in range(6)]
        self.assertEqual(statuses[:5], [201] * 5)
        self.assertEqual(statuses[5], 429)

    def test_registered_partner_gets_a_notify_link_saved(self):
        from nexus_master import partners as partners_module
        s, d = self.call("POST", "/public/partners", {
            "name": "עוד ספק", "phone": "+972501230008", "type": "supplier", "consent": True})
        self.assertEqual(s, 201)
        p = partners_module.get(self.c, self.app.tid, d["partner_id"])
        self.assertTrue(p["notify_link"].startswith("https://wa.me/972559814818"))
        self.assertEqual(p["status"], "pending")


if __name__ == "__main__":
    unittest.main()
