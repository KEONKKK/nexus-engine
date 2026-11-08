"""A/B experiments: stable variant per subject, conversions, simple winner check."""
import hashlib
import json

from . import audit


def create(conn, tenant_id, name, variants):
    if len(variants) < 2:
        raise ValueError("צריך לפחות שתי גרסאות")
    # INSERT OR REPLACE deletes-then-inserts on a (tenant_id, name) conflict, which hands the
    # row a new id and orphans any exposures already recorded against the old one. Upserting
    # keeps the id stable.
    conn.execute(
        "INSERT INTO experiments(tenant_id, name, variants, active) VALUES (?,?,?,1)"
        " ON CONFLICT(tenant_id, name) DO UPDATE SET variants=excluded.variants, active=1",
        (tenant_id, name, json.dumps(variants, ensure_ascii=False)))
    conn.commit()
    audit.log(conn, tenant_id, "user", "exp.create", {"name": name, "variants": variants})
    return conn.execute("SELECT id FROM experiments WHERE tenant_id=? AND name=?", (tenant_id, name)).fetchone()["id"]


def _exp(conn, tenant_id, name):
    r = conn.execute("SELECT * FROM experiments WHERE tenant_id=? AND name=?", (tenant_id, name)).fetchone()
    if not r:
        raise ValueError(f"ניסוי לא קיים: {name}")
    return r


def assign(conn, tenant_id, name, subject):
    """Same subject always gets the same variant."""
    e = _exp(conn, tenant_id, name)
    row = conn.execute("SELECT variant FROM exposures WHERE experiment_id=? AND subject=?",
                       (e["id"], str(subject))).fetchone()
    if row:
        return row["variant"]
    variants = json.loads(e["variants"])
    h = int(hashlib.sha256(f"{name}:{subject}".encode()).hexdigest(), 16)
    v = variants[h % len(variants)]
    conn.execute("INSERT INTO exposures(experiment_id, subject, variant) VALUES (?,?,?)", (e["id"], str(subject), v))
    conn.commit()
    return v


def convert(conn, tenant_id, name, subject, value=0):
    e = _exp(conn, tenant_id, name)
    n = conn.execute("UPDATE exposures SET converted=1, value=value+? WHERE experiment_id=? AND subject=? AND converted=0",
                     (int(value), e["id"], str(subject))).rowcount
    conn.commit()
    return bool(n)


def results(conn, tenant_id, name, min_per_variant=30):
    e = _exp(conn, tenant_id, name)
    rows = conn.execute("SELECT variant, COUNT(*) n, SUM(converted) c, SUM(value) v FROM exposures"
                        " WHERE experiment_id=? GROUP BY variant", (e["id"],)).fetchall()
    out = {r["variant"]: {"n": r["n"], "conversions": r["c"] or 0, "value": r["v"] or 0,
                          "rate_pct": round(100 * (r["c"] or 0) / r["n"], 1)} for r in rows}
    enough = out and all(x["n"] >= min_per_variant for x in out.values()) and len(out) >= 2
    leader = max(out, key=lambda k: out[k]["rate_pct"]) if out else None
    return {"variants": out, "leader": leader,
            "decision": leader if enough else None,
            "note": None if enough else f"עוד מוקדם להחליט — צריך לפחות {min_per_variant} לכל גרסה"}
