"""Kit factory: WhatsApp templates (10), CV upgrade, content scripts (12) — template based, editable."""
import json
import time

from . import audit

INDUSTRY_WORDS = {
    "beauty": {"he": "טיפוח ויופי", "service": "טיפול", "hook": "מראה מושלם בלי לצאת מהשגרה"},
    "renovation": {"he": "שיפוצים", "service": "עבודה", "hook": "בית חדש בלי כאב ראש"},
    "fitness": {"he": "כושר", "service": "אימון", "hook": "תוצאות שרואים תוך חודש"},
    "general": {"he": "שירות", "service": "שירות", "hook": "שירות שאפשר לסמוך עליו"},
}

WA_TEMPLATES = [
    ("פתיחה ללקוח חדש", "היי {name}! כאן {business} 👋 קיבלנו את הפנייה שלך ל{service}. מתי נוח לך שנדבר?"),
    ("הצעת מחיר", "היי {name}, מצרף הצעת מחיר ל{service}: ₪___. ההצעה בתוקף ל-7 ימים. רוצה שנקבע?"),
    ("תזכורת תור", "תזכורת: נפגשים מחר ב-__:__ ל{service} ב{business}. נשמח לאישור 🙏"),
    ("מעקב אחרי שתיקה", "היי {name}, רק בודק שההודעה הקודמת הגיעה. אם זה עדיין רלוונטי — אני כאן."),
    ("תודה אחרי שירות", "תודה שבחרת ב{business}! איך היה ה{service}? כל הערה עוזרת לנו להשתפר."),
    ("בקשת ביקורת", "אם היית מרוצה — נשמח לביקורת קצרה בגוגל, זה עוזר לעסק קטן כמונו המון 💙"),
    ("מבצע ללקוחות קיימים", "ללקוחות שלנו בלבד: 10% הנחה על {service} עד סוף החודש. שווה לתפוס!"),
    ("חבר מביא חבר", "מכיר מישהו שצריך {service}? שלח לו את המספר שלנו — ולשניכם הטבה."),
    ("חזרה ללקוח רדום", "היי {name}, מזמן לא דיברנו! {hook} — רוצה שנחזור לתאם?"),
    ("סגירת שבוע", "נשארו 2 מקומות פנויים השבוע ל{service} ב{business}. לשריין לך?"),
]

SCRIPT_IDEAS = [
    "3 טעויות שכולם עושים ב{he}", "לפני ואחרי — עבודה אמיתית", "שאלה שלקוחות שואלים אותנו כל יום",
    "יום עבודה אצלנו ב-30 שניות", "כמה באמת עולה {service}?", "טיפ אחד שיחסוך לכם כסף",
    "המיתוס הכי גדול על {he}", "לקוח מספר", "מה לבדוק לפני שבוחרים בעל מקצוע",
    "הכלי שאנחנו לא מוותרים עליו", "{hook}", "למה הקמתי את {business}",
]


def _fmt(s, d):
    try:
        return s.format(**d)
    except (KeyError, IndexError):
        return s


def build(kind, business, industry="general", name="{שם}", details=""):
    w = dict(INDUSTRY_WORDS.get(industry, INDUSTRY_WORDS["general"]))
    d = {"business": business, "name": name, **w}
    if kind == "whatsapp":
        return [{"title": t, "text": _fmt(x, d)} for t, x in WA_TEMPLATES]
    if kind == "scripts":
        return [{"title": _fmt(i, d), "hook": f"פתיח (3 שניות): {_fmt(i, d)}",
                 "body": f"תוכן: הסבר קצר מנקודת המבט של {business}, דוגמה אחת אמיתית.",
                 "cta": "סיום: 'שלחו לנו וואטסאפ — הקישור בביו'"} for i in SCRIPT_IDEAS]
    if kind == "cv":
        return [{"title": "תקציר מקצועי", "text": f"איש/אשת מקצוע ב{w['he']} עם ניסיון מוכח. {details}".strip()},
                {"title": "הישגים במספרים", "text": "• הגדלתי ___ ב-__%\n• טיפלתי ב-___ לקוחות בחודש"},
                {"title": "כישורים", "text": "שירות לקוחות • עבודה בצוות • עמידה ביעדים"},
                {"title": "הנחיות עיצוב", "text": "עמוד אחד, פונט אחיד, תאריכים מימין, בלי תמונה אם לא נדרש"}]
    raise ValueError("סוג ערכה לא מוכר: whatsapp / scripts / cv")


def create_kit(conn, tenant_id, kind, business, industry="general", lead_id=None, price=0, details=""):
    content = build(kind, business, industry, details=details)
    cur = conn.execute("INSERT INTO kits(tenant_id, lead_id, kind, business, industry, content, price, created)"
                       " VALUES (?,?,?,?,?,?,?,?)", (tenant_id, lead_id, kind, business, industry,
                                                     json.dumps(content, ensure_ascii=False), int(price), time.time()))
    conn.commit()
    audit.log(conn, tenant_id, "user", "kit.create", {"kit": cur.lastrowid, "kind": kind})
    return cur.lastrowid


def get_kit(conn, tenant_id, kit_id):
    r = conn.execute("SELECT * FROM kits WHERE tenant_id=? AND id=?", (tenant_id, kit_id)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["content"] = json.loads(d["content"])
    return d


def as_whatsapp_parts(kit, max_len=3500):
    """Split a kit into WhatsApp-sized messages."""
    parts, cur = [], ""
    for item in kit["content"]:
        block = f"*{item['title']}*\n" + "\n".join(str(v) for k, v in item.items() if k != "title") + "\n\n"
        if len(cur) + len(block) > max_len and cur:
            parts.append(cur.strip())
            cur = ""
        cur += block
    if cur:
        parts.append(cur.strip())
    return parts


def deliver(conn, tenant_id, kit_id, amount_paid):
    """Mark delivered and record the sale once."""
    k = get_kit(conn, tenant_id, kit_id)
    if not k:
        raise ValueError("ערכה לא קיימת")
    if k["status"] == "delivered":
        raise ValueError("הערכה כבר נמסרה ונרשמה")
    conn.execute("UPDATE kits SET status='delivered', delivered=?, price=? WHERE id=?",
                 (time.time(), int(amount_paid), kit_id))
    if k["lead_id"]:
        conn.execute("UPDATE leads SET status='won' WHERE id=?", (k["lead_id"],))
    conn.commit()
    audit.log(conn, tenant_id, "user", "kit.deliver", {"kit": kit_id, "amount": int(amount_paid)})
    return True


def kit_revenue(conn, tenant_id):
    return conn.execute("SELECT IFNULL(SUM(price),0) FROM kits WHERE tenant_id=? AND status='delivered'",
                        (tenant_id,)).fetchone()[0]
