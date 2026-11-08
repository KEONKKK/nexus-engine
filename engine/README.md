# NEXUS מאסטר — מנוע מרכז הלידים (v2.0)

פייתון 3.10+ בלבד, בלי ספריות חיצוניות. נתונים ב־SQLite.

## מה יש בפנים
| מודול | מה הוא עושה |
|---|---|
| `scoring.py` | קליטת טקסט חופשי (פוסט/הודעה) → שירות, אזור, תקציב, דחיפות, טלפון; 5 ציונים (כוונה, דחיפות, תקציב, התאמה, יצירת קשר) עם נימוק לכל אחד |
| `leads.py` | הוספת ליד, זיהוי כפילויות (אותו טלפון+שירות ב־7 ימים), סטטוסים, מחיקת דמו |
| `buyers.py` | עסקים שקונים לידים, יומן קרדיט (טעינה = כסף שהתקבל בפועל), מכירת ליד פעם אחת בלבד לכל קונה |
| `brokerage.py` | ספקים, התאמה כנה לפי מילות מפתח + אזור + דירוג, סגירת עסקה פעם אחת עם הסכום האמיתי, עמלה, למידה (סגירה מעלה דירוג, דחייה מורידה), סימולטור עמלות (מסומן כסימולציה) |
| `attribution.py` | מקור הליד מ־`#ref` (wa_friends, fb_biz, fb_pro, status) ודוח הכנסות לפי מקור |
| `experiments.py` | ניסויי A/B: גרסה קבועה לכל משתמש, המרות, החלטה רק אחרי מספיק נתונים |
| `kits.py` | מפעל ערכות: 10 תבניות וואטסאפ / שדרוג קורות חיים / 12 תסריטי תוכן, לפי תחום (beauty / renovation / fitness), חלוקה להודעות וואטסאפ, מסירה ורישום מכירה פעם אחת |
| `whatsapp.py` | מצב ידני (קישורי wa.me — פעיל) ו־Cloud API של Meta (בנוי, כבוי עד שיש WA_TOKEN ו־WA_PHONE_ID) |
| `guardian.py` | חוקי שליחה: מספר תקין, לא מספר דמה, הסכמה, רשימת הסרה ("הסר"), עד 3 הודעות ביום לנמען. מספר העסק: 055-981-4818 |
| `audit.py` | יומן פעולות בשרשרת גיבוב — כל שינוי ידני נחשף ב־`verify` |
| `command.py` | מרכז פקודות בעברית |
| `api.py` | API ב־JSON (WSGI) |

## הרצה
```bash
python -m nexus_master demo                    # נתוני דמו (מסומנים is_demo)
python -m nexus_master add "מחפש אינסטלטור דחוף בחולון 050-1234567"
python -m nexus_master cmd "לידים חמים"
python -m nexus_master cmd "לוח"
python -m nexus_master verify                   # בדיקת יומן הפעולות
python -m nexus_master serve --port 8304        # שרת API (B; חלון A על 8303). אין ברירת מחדל -- חובה
python -m unittest discover -s engine/tests -t engine   # בדיקות (מהשורש), או מתוך engine/: -s tests -t .
```

## פקודות
`לוח` · `לידים חמים` · `מקורות` · `התאם ליד 5` · `מכור ליד 5 לקונה 2` · `סגור עסקה 3 1500` · `סמן ליד 5 lost` · `הוסף ליד <טקסט>`

## API עיקרי
`GET /health` · `GET /dashboard` · `GET/POST /leads` · `POST /buyers` · `/buyers/topup` · `/buyers/sell` ·
`POST /suppliers` · `POST /deals` · `/deals/close` · `/simulate` · `GET /attribution` · `POST /kits` · `/kits/deliver` ·
`/wa/link` · `/experiments/assign` · `/command`

אבטחה: להגדיר `NEXUS_API_KEY` ולשלוח בכותרת `X-API-Key`. בלי מפתח השרת עונה רק מהמחשב עצמו (localhost).

## מה עוד צריך כדי לעלות לאוויר
- חשבון אחסון (למשל Fly.io / Render) והגדרת `NEXUS_API_KEY`
- אישור Meta ל־WhatsApp Cloud API (`WA_TOKEN`, `WA_PHONE_ID`) — עד אז שולחים ידנית דרך wa.me
- מחירי לידים אמיתיים לכל קונה

אין כאן הכנסות, לקוחות או פריסה שלא קרו — כל הסכומים נרשמים רק כשמזינים כסף שהתקבל בפועל.
