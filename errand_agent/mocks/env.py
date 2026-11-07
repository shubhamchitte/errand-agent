"""MockEnvironment: the 8 fixed tools, state persisted in SQLite."""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .. import db
from ..clock import Clock, RealClock
from . import data
from .errors import ToolError, ToolTimeout, TransientError
from .scenario import Scenario

DDL = """
CREATE TABLE IF NOT EXISTS mock_meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS mock_holds(hold_id TEXT PRIMARY KEY, provider_id TEXT, slot TEXT,
  quoted_price INTEGER, search_price INTEGER, created_at TEXT, expires_at TEXT);
CREATE TABLE IF NOT EXISTS mock_bookings(booking_id TEXT PRIMARY KEY, user_id TEXT, provider_id TEXT,
  provider_name TEXT, service TEXT, slot TEXT, status TEXT, price INTEGER, hold_id TEXT,
  idempotency_key TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS mock_idem(tool TEXT, key TEXT, result TEXT, PRIMARY KEY(tool, key));
CREATE TABLE IF NOT EXISTS mock_calls(tool TEXT PRIMARY KEY, n INTEGER);
CREATE TABLE IF NOT EXISTS mock_cancel_attempts(booking_id TEXT PRIMARY KEY, n INTEGER);
CREATE TABLE IF NOT EXISTS mock_taken(provider_id TEXT, slot TEXT, PRIMARY KEY(provider_id, slot));
CREATE TABLE IF NOT EXISTS mock_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, booking_id TEXT,
  provider_id TEXT, payload TEXT, created_at TEXT, delivered INTEGER DEFAULT 0);
"""
HORIZON_DAYS = 14
DEFAULT_USER = "u_demo"


class MockEnvironment:
    def __init__(self, conn: sqlite3.Connection | None = None, clock: Clock | None = None,
                 scenario: Scenario | None = None, user_id: str = DEFAULT_USER):
        self.conn = conn or db.connect()
        self.clock = clock or RealClock()
        self.scenario = scenario or Scenario()
        self.user_id = user_id
        db.init_schema(self.conn, DDL)
        self._seed()

    # ---------------------------------------------------------------- helpers
    def _next(self, key: str) -> int:
        r = db.one(self.conn, "SELECT v FROM mock_meta WHERE k=?", (key,))
        n = int(r["v"]) + 1 if r else 1
        self.conn.execute("INSERT OR REPLACE INTO mock_meta VALUES(?,?)", (key, str(n)))
        return n

    def _count_call(self, tool: str) -> int:
        r = db.one(self.conn, "SELECT n FROM mock_calls WHERE tool=?", (tool,))
        n = (r["n"] if r else 0) + 1
        self.conn.execute("INSERT OR REPLACE INTO mock_calls VALUES(?,?)", (tool, n))
        self.conn.commit()
        return n

    def call_count(self, tool: str) -> int:
        r = db.one(self.conn, "SELECT n FROM mock_calls WHERE tool=?", (tool,))
        return r["n"] if r else 0

    def _rng(self, tool: str, n: int) -> random.Random:
        # Stateless per-call RNG: reproducible and independent of restarts.
        return random.Random(f"{self.scenario.seed}:{tool}:{n}")

    def _idem_get(self, tool: str, key: str) -> dict | None:
        r = db.one(self.conn, "SELECT result FROM mock_idem WHERE tool=? AND key=?", (tool, key))
        return json.loads(r["result"]) if r else None

    def _idem_put(self, tool: str, key: str, result: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO mock_idem VALUES(?,?,?)", (tool, key, json.dumps(result)))

    def _pre(self, tool: str) -> tuple[int, str | None]:
        """Count call, apply forced generic failures. Returns (n, forced_outcome)."""
        n = self._count_call(tool)
        forced = self.scenario.forced.get((tool, n))
        if forced == "transient_error":
            raise TransientError(f"{tool}: service unavailable (503)")
        if forced == "timeout":
            raise ToolTimeout(f"{tool}: timed out")
        return n, forced

    def _seed(self) -> None:
        if db.one(self.conn, "SELECT v FROM mock_meta WHERE k='seeded'"):
            return
        today = self.clock.now().date()
        seeds = [
            ("bk_seed_1", "p_elc_1", today - timedelta(days=30), "10:00", "completed"),
            ("bk_seed_2", "p_plm_1", today - timedelta(days=60), "09:30", "completed"),
            ("bk_seed_3", "p_plm_3", today - timedelta(days=12), "11:00", "cancelled"),
            ("bk_seed_4", "p_ac_2", today + timedelta(days=3), "12:30", "confirmed"),
        ]
        for bid, pid, d, t, st in seeds:
            p = data.BY_ID[pid]
            self.conn.execute(
                "INSERT INTO mock_bookings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (bid, DEFAULT_USER, pid, p["name"], p["service"], f"{d.isoformat()}T{t}", st, p["price_inr"],
                 None, None, self.clock.iso()))
        self.conn.execute("INSERT INTO mock_meta VALUES('seeded','1')")
        self.conn.commit()

    def _slots(self, provider: dict, start: str | None = None, end: str | None = None) -> list[str]:
        today = self.clock.now().date()
        out = []
        for d in range(1, HORIZON_DAYS + 1):
            day = today + timedelta(days=d)
            if day.weekday() == 6 or (provider["attributes"].get("weekdays_only") and day.weekday() == 5):
                continue
            ds = day.isoformat()
            if (start and ds < start) or (end and ds > end):
                continue
            out += [f"{ds}T{t}" for t in provider["times"]]
        busy = {r["slot"] for r in db.rows(
            self.conn, "SELECT slot FROM mock_bookings WHERE provider_id=? AND status='confirmed'", (provider["provider_id"],))}
        busy |= {r["slot"] for r in db.rows(self.conn, "SELECT slot FROM mock_taken WHERE provider_id=?", (provider["provider_id"],))}
        return [s for s in out if s not in busy and s > self.clock.now().isoformat(timespec="minutes")]

    @staticmethod
    def _parse_range(date_range: Any) -> tuple[str | None, str | None]:
        if not date_range:
            return None, None
        if isinstance(date_range, dict):
            s, e = date_range.get("start"), date_range.get("end")
            return s, e or s
        if isinstance(date_range, (list, tuple)):
            return date_range[0], date_range[-1]
        parts = [x.strip() for x in str(date_range).replace(" to ", "/").split("/") if x.strip()]
        return (parts[0], parts[-1]) if parts else (None, None)

    # ------------------------------------------------------------------ tools
    def search_providers(self, service: str, area: str | None = None, date_range: Any = None) -> dict:
        self._pre("search_providers")
        svc = data.SERVICE_ALIASES.get(str(service).strip().lower())
        if not svc:
            return {"status": "unknown_service", "providers": [],
                    "supported_services": ["plumber", "electrician", "ac_servicing"]}
        start, end = self._parse_range(date_range)
        cands = []
        for p in data.PROVIDERS:
            if p["service"] != svc:
                continue
            slots = self._slots(p, start, end)
            if not slots:
                continue
            cands.append({"provider_id": p["provider_id"], "name": p["name"], "service": svc, "area": p["area"],
                          "price_inr": p["price_inr"], "rating": p["rating"], "review_count": p["review_count"],
                          "available_slots": slots[:12], "attributes": p["attributes"],
                          "in_requested_area": bool(area) and p["area"].lower() == str(area).lower()})
        cands.sort(key=lambda c: (not c["in_requested_area"], -c["rating"]))
        return {"status": "ok", "providers": cands[:8]}

    def get_provider(self, provider_id: str) -> dict:
        self._pre("get_provider")
        p = data.BY_ID.get(provider_id)
        if not p:
            return {"status": "not_found", "provider_id": provider_id}
        reviews = [{"author": a, "rating": r, "text": t} for a, r, t in data.GENERIC_REVIEWS[: 2 + len(provider_id) % 3]]
        if provider_id in data.INJECTION_REVIEWS:
            a, r, t = data.INJECTION_REVIEWS[provider_id]
            reviews.insert(1, {"author": a, "rating": r, "text": t})
        return {"status": "ok", "provider_id": provider_id, "name": p["name"], "service": p["service"],
                "area": p["area"], "price_inr": p["price_inr"], "rating": p["rating"],
                "review_count": p["review_count"], "attributes": p["attributes"],
                "description": f"{p['name']} - {p['service'].replace('_', ' ')} specialists in {p['area']}.",
                "reviews": reviews}

    def hold_slot(self, provider_id: str, slot: str) -> dict:
        self._pre("hold_slot")
        p = data.BY_ID.get(provider_id)
        if not p:
            return {"status": "not_found", "provider_id": provider_id}
        if slot not in self._slots(p):
            return {"status": "slot_unavailable", "provider_id": provider_id, "slot": slot}
        sc = self.scenario
        drift = sc.provider_quote_drift.get(provider_id, sc.quote_drift)
        now = self.clock.now()
        hid = f"hold_{self._next('hold_seq'):04d}"
        exp = (now + timedelta(minutes=sc.hold_minutes)).isoformat(timespec="seconds")
        self.conn.execute("INSERT INTO mock_holds VALUES(?,?,?,?,?,?,?)",
                          (hid, provider_id, slot, p["price_inr"] + drift, p["price_inr"], now.isoformat(timespec="seconds"), exp))
        self.conn.commit()
        return {"status": "held", "hold_id": hid, "provider_id": provider_id, "slot": slot,
                "quoted_price": p["price_inr"] + drift, "search_price": p["price_inr"], "currency": "INR",
                "expires_at": exp}

    def _booking_by_key(self, key: str) -> dict | None:
        return db.one(self.conn, "SELECT * FROM mock_bookings WHERE idempotency_key=?", (key,))

    @staticmethod
    def _bk_view(b: dict, status: str = "confirmed") -> dict:
        return {"status": status, "booking_id": b["booking_id"], "hold_id": b["hold_id"],
                "provider_id": b["provider_id"], "slot": b["slot"], "price": b["price"], "currency": "INR"}

    def _make_booking(self, hold: dict, key: str) -> dict:
        p = data.BY_ID[hold["provider_id"]]
        bid = f"bk_{self._next('booking_seq'):04d}"
        self.conn.execute("INSERT INTO mock_bookings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (bid, self.user_id, p["provider_id"], p["name"], p["service"], hold["slot"], "confirmed",
                           hold["quoted_price"], hold["hold_id"], key, self.clock.iso()))
        return self._booking_by_key(key)

    def book(self, hold_id: str, idempotency_key: str) -> dict:
        n, forced = self._pre("book")
        # Idempotent replay: an existing booking under this key is always returned as-is.
        prior = self._booking_by_key(idempotency_key)
        if prior:
            return self._bk_view(prior)
        memo = self._idem_get("book", idempotency_key)
        if memo:
            return memo
        hold = db.one(self.conn, "SELECT * FROM mock_holds WHERE hold_id=?", (hold_id,))
        if not hold:
            return {"status": "unknown_hold", "hold_id": hold_id}
        if forced == "hold_expired" or self.clock.iso() > hold["expires_at"]:
            res = {"status": "hold_expired", "hold_id": hold_id, "expired_at": hold["expires_at"]}
            self._idem_put("book", idempotency_key, res)
            self.conn.commit()
            return res
        other = db.one(self.conn, "SELECT 1 x FROM mock_bookings WHERE hold_id=?", (hold_id,))
        taken = db.one(self.conn, "SELECT 1 x FROM mock_taken WHERE provider_id=? AND slot=?", (hold["provider_id"], hold["slot"]))
        if other or taken:
            res = {"status": "slot_taken", "hold_id": hold_id}
            self._idem_put("book", idempotency_key, res)
            self.conn.commit()
            return res
        outcome = forced
        if not outcome:
            r = self._rng("book", n).random()
            sc = self.scenario
            if r < sc.p_success:
                outcome = "success"
            elif r < sc.p_success + sc.p_slot_taken:
                outcome = "slot_taken"
            else:
                rest = (sc.p_success + sc.p_slot_taken)
                outcome = "timeout_booked" if r < rest + (1 - rest) / 2 else "timeout_not_booked"
        if outcome == "slot_taken":
            self.conn.execute("INSERT OR IGNORE INTO mock_taken VALUES(?,?)", (hold["provider_id"], hold["slot"]))
            res = {"status": "slot_taken", "hold_id": hold_id}
            self._idem_put("book", idempotency_key, res)
            self.conn.commit()
            return res
        if outcome in ("success", "timeout_booked"):
            b = self._make_booking(hold, idempotency_key)
            self.conn.commit()
            if outcome == "timeout_booked":
                raise ToolTimeout("book: timed out (outcome unknown)")
            return self._bk_view(b)
        self.conn.commit()  # timeout_not_booked: nothing persisted
        raise ToolTimeout("book: timed out (outcome unknown)")

    def get_booking_status(self, hold_id: str | None = None, idempotency_key: str | None = None) -> dict:
        self._pre("get_booking_status")
        if not hold_id and not idempotency_key:
            return {"status": "error", "message": "provide hold_id or idempotency_key"}
        b = None
        if idempotency_key:
            b = self._booking_by_key(idempotency_key)
        if not b and hold_id:
            b = db.one(self.conn, "SELECT * FROM mock_bookings WHERE hold_id=?", (hold_id,))
        if b:
            return {**self._bk_view(b, "booked" if b["status"] == "confirmed" else b["status"]),
                    "booking_status": b["status"]}
        res: dict = {"status": "not_booked", "hold_id": hold_id, "idempotency_key": idempotency_key}
        if hold_id:
            h = db.one(self.conn, "SELECT * FROM mock_holds WHERE hold_id=?", (hold_id,))
            res["hold_state"] = ("unknown" if not h else "expired" if self.clock.iso() > h["expires_at"] else "active")
            if h:
                res["expires_at"] = h["expires_at"]
        return res

    def cancel(self, booking_id: str, idempotency_key: str) -> dict:
        n, forced = self._pre("cancel")
        memo = self._idem_get("cancel", idempotency_key)
        if memo:
            return memo
        b = db.one(self.conn, "SELECT * FROM mock_bookings WHERE booking_id=?", (booking_id,))
        if not b:
            return {"status": "not_found", "booking_id": booking_id}
        if self.scenario.cancel_transient_first:
            a = db.one(self.conn, "SELECT n FROM mock_cancel_attempts WHERE booking_id=?", (booking_id,))
            if not a:
                self.conn.execute("INSERT INTO mock_cancel_attempts VALUES(?,1)", (booking_id,))
                self.conn.commit()
                raise TransientError("cancel: service unavailable (503)")
        if b["status"] != "confirmed":
            res = {"status": "not_cancellable", "booking_id": booking_id, "booking_status": b["status"]}
        else:
            self.conn.execute("UPDATE mock_bookings SET status='cancelled' WHERE booking_id=?", (booking_id,))
            res = {"status": "cancelled", "booking_id": booking_id}
        self._idem_put("cancel", idempotency_key, res)
        self.conn.commit()
        return res

    def reschedule(self, booking_id: str, slot: str, idempotency_key: str) -> dict:
        n, forced = self._pre("reschedule")
        memo = self._idem_get("reschedule", idempotency_key)
        if memo:
            return memo
        b = db.one(self.conn, "SELECT * FROM mock_bookings WHERE booking_id=?", (booking_id,))
        if not b:
            return {"status": "not_found", "booking_id": booking_id}
        if b["status"] != "confirmed":
            return {"status": "not_reschedulable", "booking_id": booking_id, "booking_status": b["status"]}
        p = data.BY_ID[b["provider_id"]]
        avail = self._slots(p)
        if forced == "slot_conflict" or (forced != "success" and (slot in self.scenario.conflict_slots or slot not in avail)):
            alts = [s for s in avail if s != slot and s not in self.scenario.conflict_slots][:3]
            res = {"status": "slot_conflict", "booking_id": booking_id, "requested_slot": slot, "alternatives": alts}
        else:
            self.conn.execute("UPDATE mock_bookings SET slot=? WHERE booking_id=?", (slot, booking_id))
            res = {"status": "rescheduled", "booking_id": booking_id, "slot": slot, "previous_slot": b["slot"],
                   "provider_id": b["provider_id"]}
        self._idem_put("reschedule", idempotency_key, res)
        self.conn.commit()
        return res

    def list_bookings(self, user_id: str) -> dict:
        self._pre("list_bookings")
        rs = db.rows(self.conn, "SELECT * FROM mock_bookings WHERE user_id=? ORDER BY slot", (user_id,))
        return {"status": "ok", "user_id": user_id, "bookings": [
            {"booking_id": r["booking_id"], "provider_id": r["provider_id"], "provider_name": r["provider_name"],
             "service": r["service"], "slot": r["slot"], "status": r["status"], "price": r["price"]} for r in rs]}

    # ------------------------------------------------------------ event queue
    def emit_provider_cancellation(self, booking_id: str) -> dict:
        """Simulate the provider cancelling a booking: status -> cancelled_by_provider + queued event."""
        b = db.one(self.conn, "SELECT * FROM mock_bookings WHERE booking_id=?", (booking_id,))
        if not b:
            raise KeyError(booking_id)
        self.conn.execute("UPDATE mock_bookings SET status='cancelled_by_provider' WHERE booking_id=?", (booking_id,))
        payload = {"booking_id": booking_id, "provider_id": b["provider_id"], "service": b["service"], "slot": b["slot"]}
        cur = self.conn.execute(
            "INSERT INTO mock_events(type,booking_id,provider_id,payload,created_at) VALUES(?,?,?,?,?)",
            ("provider_cancelled", booking_id, b["provider_id"], json.dumps(payload), self.clock.iso()))
        self.conn.commit()
        return {"event_id": cur.lastrowid, "type": "provider_cancelled", **payload}

    def poll_events(self) -> list[dict]:
        """Return undelivered events (once each), oldest first."""
        rs = db.rows(self.conn, "SELECT * FROM mock_events WHERE delivered=0 ORDER BY event_id")
        for r in rs:
            self.conn.execute("UPDATE mock_events SET delivered=1 WHERE event_id=?", (r["event_id"],))
        self.conn.commit()
        return [{"event_id": r["event_id"], "type": r["type"], "created_at": r["created_at"], **json.loads(r["payload"])}
                for r in rs]

    # --------------------------------------------------------------- dispatch
    def call(self, name: str, **args: Any) -> dict:
        """Invoke a tool by name (raises ToolError subclasses on 503/timeout)."""
        if name not in TOOL_NAMES:
            raise ToolError(f"unknown tool {name}")
        return getattr(self, name)(**args)


TOOL_NAMES = ["search_providers", "get_provider", "hold_slot", "book", "get_booking_status", "cancel",
              "reschedule", "list_bookings"]
