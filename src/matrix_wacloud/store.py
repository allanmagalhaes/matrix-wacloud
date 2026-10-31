"""SQLite persistence: room links, subscribers (with consent), alerts,
per-recipient deliveries and the raw status events received from Meta.

SQLite is enough for the expected volume (one institution, a few thousand
recipients at most) and keeps deployment to a single file.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS room_link (
    room_id     TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    template    TEXT NOT NULL,
    language    TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriber (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id         TEXT NOT NULL,
    phone           TEXT NOT NULL,
    consent_source  TEXT NOT NULL,
    consent_at      INTEGER NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    opted_out_at    INTEGER,
    UNIQUE (room_id, phone)
);
CREATE TABLE IF NOT EXISTS alert (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     TEXT NOT NULL,
    event_id    TEXT NOT NULL UNIQUE,
    sender      TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id     INTEGER NOT NULL REFERENCES alert(id),
    phone        TEXT NOT NULL,
    wamid        TEXT UNIQUE,
    status       TEXT NOT NULL,          -- queued, accepted, sent, delivered, read, failed
    error_code   INTEGER,
    error_title  TEXT,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    sent_at      INTEGER,
    delivered_at INTEGER,
    read_at      INTEGER
);
CREATE TABLE IF NOT EXISTS status_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wamid       TEXT NOT NULL,
    status      TEXT NOT NULL,
    ts          INTEGER,
    raw         TEXT NOT NULL,
    received_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delivery_alert ON delivery(alert_id);
CREATE INDEX IF NOT EXISTS idx_subscriber_phone ON subscriber(phone);
"""

# A status update must never move a delivery "backwards" (Meta webhooks can
# arrive out of order, e.g. 'delivered' after 'read').
STATUS_RANK = {"queued": 0, "accepted": 1, "sent": 2, "delivered": 3, "read": 4, "failed": 5}


@dataclass
class RoomLink:
    room_id: str
    label: str
    template: str
    language: str


def now() -> int:
    return int(time.time())


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ---- room links -------------------------------------------------------
    def link_room(self, room_id: str, label: str, template: str, language: str, by: str) -> None:
        self.db.execute(
            "INSERT INTO room_link(room_id,label,template,language,created_by,created_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(room_id) DO UPDATE SET "
            "label=excluded.label, template=excluded.template, language=excluded.language",
            (room_id, label, template, language, by, now()),
        )
        self.db.commit()

    def unlink_room(self, room_id: str) -> bool:
        cur = self.db.execute("DELETE FROM room_link WHERE room_id=?", (room_id,))
        self.db.commit()
        return cur.rowcount > 0

    def get_link(self, room_id: str) -> RoomLink | None:
        r = self.db.execute(
            "SELECT room_id,label,template,language FROM room_link WHERE room_id=?", (room_id,)
        ).fetchone()
        return RoomLink(**dict(r)) if r else None

    # ---- subscribers ------------------------------------------------------
    def add_subscriber(self, room_id: str, phone: str, consent_source: str,
                       consent_at: int | None = None) -> bool:
        """Insert or re-activate. Returns True if the subscriber is new."""
        existing = self.db.execute(
            "SELECT id FROM subscriber WHERE room_id=? AND phone=?", (room_id, phone)
        ).fetchone()
        ts = consent_at or now()
        if existing:
            self.db.execute(
                "UPDATE subscriber SET active=1, opted_out_at=NULL, consent_source=?, consent_at=? "
                "WHERE id=?", (consent_source, ts, existing["id"]),
            )
        else:
            self.db.execute(
                "INSERT INTO subscriber(room_id,phone,consent_source,consent_at) VALUES(?,?,?,?)",
                (room_id, phone, consent_source, ts),
            )
        self.db.commit()
        return existing is None

    def remove_subscriber(self, room_id: str, phone: str) -> bool:
        cur = self.db.execute(
            "UPDATE subscriber SET active=0, opted_out_at=? WHERE room_id=? AND phone=? AND active=1",
            (now(), room_id, phone),
        )
        self.db.commit()
        return cur.rowcount > 0

    def opt_out_everywhere(self, phone: str) -> int:
        cur = self.db.execute(
            "UPDATE subscriber SET active=0, opted_out_at=? WHERE phone=? AND active=1",
            (now(), phone),
        )
        self.db.commit()
        return cur.rowcount

    def active_subscribers(self, room_id: str) -> list[str]:
        rows = self.db.execute(
            "SELECT phone FROM subscriber WHERE room_id=? AND active=1 ORDER BY id", (room_id,)
        ).fetchall()
        return [r["phone"] for r in rows]

    # ---- alerts & deliveries ---------------------------------------------
    def create_alert(self, room_id: str, event_id: str, sender: str, text: str) -> int | None:
        """Returns the alert id, or None if this event was already processed
        (the homeserver may retry a transaction)."""
        try:
            cur = self.db.execute(
                "INSERT INTO alert(room_id,event_id,sender,text,created_at) VALUES(?,?,?,?,?)",
                (room_id, event_id, sender, text, now()),
            )
        except sqlite3.IntegrityError:
            return None
        self.db.commit()
        return cur.lastrowid

    def create_delivery(self, alert_id: int, phone: str) -> int:
        t = now()
        cur = self.db.execute(
            "INSERT INTO delivery(alert_id,phone,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            (alert_id, phone, "queued", t, t),
        )
        self.db.commit()
        return cur.lastrowid

    def mark_accepted(self, delivery_id: int, wamid: str) -> None:
        self.db.execute(
            "UPDATE delivery SET wamid=?, status='accepted', updated_at=? WHERE id=?",
            (wamid, now(), delivery_id),
        )
        self.db.commit()

    def mark_failed(self, delivery_id: int, code: int | None, title: str | None) -> None:
        self.db.execute(
            "UPDATE delivery SET status='failed', error_code=?, error_title=?, updated_at=? WHERE id=?",
            (code, title, now(), delivery_id),
        )
        self.db.commit()

    def apply_status(self, wamid: str, status: str, ts: int | None, raw: dict,
                     error_code: int | None = None, error_title: str | None = None) -> bool:
        """Record a webhook status. Returns True if it matched a known delivery."""
        self.db.execute(
            "INSERT INTO status_event(wamid,status,ts,raw,received_at) VALUES(?,?,?,?,?)",
            (wamid, status, ts, json.dumps(raw, ensure_ascii=False), now()),
        )
        row = self.db.execute("SELECT id,status FROM delivery WHERE wamid=?", (wamid,)).fetchone()
        if row is None:
            self.db.commit()
            return False
        col = {"sent": "sent_at", "delivered": "delivered_at", "read": "read_at"}.get(status)
        if col:
            self.db.execute(f"UPDATE delivery SET {col}=COALESCE({col},?) WHERE id=?", (ts, row["id"]))
            if status == "read":  # a read message was necessarily delivered
                self.db.execute(
                    "UPDATE delivery SET delivered_at=COALESCE(delivered_at,?) WHERE id=?", (ts, row["id"])
                )
        if STATUS_RANK.get(status, -1) > STATUS_RANK.get(row["status"], -1):
            self.db.execute(
                "UPDATE delivery SET status=?, error_code=COALESCE(?,error_code), "
                "error_title=COALESCE(?,error_title), updated_at=? WHERE id=?",
                (status, error_code, error_title, now(), row["id"]),
            )
        self.db.commit()
        return True

    def last_alert(self, room_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM alert WHERE room_id=? ORDER BY id DESC LIMIT 1", (room_id,)
        ).fetchone()

    def delivery_summary(self, alert_id: int) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT status, COUNT(*) AS n FROM delivery WHERE alert_id=? GROUP BY status", (alert_id,)
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def failures(self, alert_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT phone,error_code,error_title FROM delivery WHERE alert_id=? AND status='failed'",
            (alert_id,),
        ).fetchall()
