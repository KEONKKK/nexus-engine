"""Hash-chained audit log: every action is linked to the previous one, so edits are detectable."""
import hashlib
import json
import time

GENESIS = "0" * 64


def _digest(prev, ts, actor, action, data):
    raw = json.dumps([prev, round(ts, 6), actor, action, data], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def log(conn, tenant_id, actor, action, data=None):
    data_s = json.dumps(data or {}, ensure_ascii=False, sort_keys=True)
    row = conn.execute("SELECT hash FROM audit WHERE tenant_id=? ORDER BY id DESC LIMIT 1",
                       (tenant_id,)).fetchone()
    prev = row["hash"] if row else GENESIS
    ts = time.time()
    h = _digest(prev, ts, actor, action, data_s)
    conn.execute("INSERT INTO audit(tenant_id, ts, actor, action, data, prev_hash, hash) "
                 "VALUES (?,?,?,?,?,?,?)", (tenant_id, round(ts, 6), actor, action, data_s, prev, h))
    conn.commit()
    return h


def verify(conn, tenant_id):
    """Return (ok, bad_id). ok=True when the whole chain is intact."""
    prev = GENESIS
    for r in conn.execute("SELECT * FROM audit WHERE tenant_id=? ORDER BY id", (tenant_id,)):
        if r["prev_hash"] != prev or _digest(prev, r["ts"], r["actor"], r["action"], r["data"]) != r["hash"]:
            return False, r["id"]
        prev = r["hash"]
    return True, None
