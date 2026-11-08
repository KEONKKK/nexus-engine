"""Section 69: customizable sales call scripts -- talk-tracks for the human seller
(opening / objection / closing), distinct from kits.SCRIPT_IDEAS (content-marketing post
ideas, a different product entirely -- see design/STATE-prep.md's "ערכה" glossary
decision). "Customizable" means a tenant can override any scenario's text via the
existing db.settings table (no new table needed) -- the hardcoded dict below is only the
default that applies until a tenant sets one."""
import json

from .. import db

DEFAULT_SCRIPTS = {
    "opening": "תודה שהשארת פרטים! אני כאן כדי להבין מה אתה צריך ולמצוא לך את ההתאמה הטובה ביותר.",
    "objection": "אני מבין/ה את הנקודה. בוא נבדוק יחד מה הכי מתאים לך.",
    "closing": "נשמע שזה מתאים? אפשר לקבוע מועד ולהתחיל.",
}

_SETTINGS_KEY = "sales_ai_scripts"


def get_script(conn, tenant_id, scenario):
    overrides = json.loads(db.get_setting(conn, tenant_id, _SETTINGS_KEY, "{}"))
    if scenario in overrides:
        return overrides[scenario]
    if scenario not in DEFAULT_SCRIPTS:
        raise ValueError(f"תסריט לא מוכר: {scenario} (זמינים: {', '.join(DEFAULT_SCRIPTS)})")
    return DEFAULT_SCRIPTS[scenario]


def set_script(conn, tenant_id, scenario, text):
    overrides = json.loads(db.get_setting(conn, tenant_id, _SETTINGS_KEY, "{}"))
    overrides[scenario] = text
    db.set_setting(conn, tenant_id, _SETTINGS_KEY, json.dumps(overrides, ensure_ascii=False))
