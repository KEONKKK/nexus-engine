"""CLI:
  python -m nexus_master serve --port 8304 [--db nexus.db]
  python -m nexus_master cmd "לידים חמים"
  python -m nexus_master add "מחפש אינסטלטור דחוף בחולון 050-1234567"
  python -m nexus_master demo        # demo data (flagged is_demo)
  python -m nexus_master verify      # check the audit chain
  python -m nexus_master tick        # run one batch of due background tasks, then exit
"""
import argparse
import json
import sys

from . import audit, brokerage, command, db, leads
from .core import tasks as core_tasks
from .dispatcher import Dispatcher


def main(argv=None):
    p = argparse.ArgumentParser(prog="nexus_master")
    p.add_argument("action", choices=["serve", "cmd", "add", "demo", "verify", "tick"])
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--db", default="nexus.db")
    # No default on purpose, same as tools/static-server.mjs: NEXUS pins 8303
    # (window A) / 8304 (window B), and a server that guesses its port is one
    # that can silently collide with someone else's.
    p.add_argument("--port", type=int, default=None)
    a = p.parse_args(argv)
    if a.action == "serve" and a.port is None:
        p.error("--port is required for serve (no default on purpose -- pass 8303 or 8304)")
    conn = db.connect(a.db)
    tid = db.ensure_tenant(conn)
    out = lambda x: print(json.dumps(x, ensure_ascii=False, indent=2, default=str))

    if a.action == "serve":
        from wsgiref.simple_server import make_server
        print(f"NEXUS מאסטר API → http://localhost:{a.port}  (Ctrl+C לעצירה)")
        make_server("", a.port, Dispatcher(conn)).serve_forever()
    elif a.action == "cmd":
        out(command.run(conn, tid, a.text))
    elif a.action == "add":
        lid, dup = leads.add_lead(conn, tid, text=a.text, consent=True, actor="cli")
        out({"lead": leads.get(conn, tid, lid) if lid else None, "duplicate_of": dup})
    elif a.action == "demo":
        brokerage.add_supplier(conn, tid, "יוסי אינסטלציה (דמו)", "0501111111", "אינסטלטור,נזילה,דוד",
                               "חולון,בת ים", is_demo=True)
        brokerage.add_supplier(conn, tid, "אור חשמל (דמו)", "0502222222", "חשמלאי,לוח חשמל", "", is_demo=True)
        leads.add_lead(conn, tid, text="מחפש אינסטלטור דחוף בחולון, נזילה מהדוד", phone="0523333333",
                       consent=True, is_demo=True)
        out(command.dashboard(conn, tid))
    elif a.action == "verify":
        ok, bad = audit.verify(conn, tid)
        out({"audit_intact": ok, "first_bad_id": bad})
        sys.exit(0 if ok else 1)
    elif a.action == "tick":
        # One batch, then exit -- not a daemon. A scheduler (cron, a scheduled task, a
        # human) decides *when* to run this; this command only decides what to do once run.
        out(core_tasks.tick(conn, tid))


if __name__ == "__main__":
    main()
