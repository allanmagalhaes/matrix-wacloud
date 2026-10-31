import pytest

from matrix_wacloud.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_link_and_subscribers(store):
    store.link_room("!r:x", "BSI", "aviso", "pt_BR", "@a:x")
    assert store.get_link("!r:x").label == "BSI"
    store.link_room("!r:x", "BSI-2", "aviso", "pt_BR", "@a:x")  # upsert
    assert store.get_link("!r:x").label == "BSI-2"

    assert store.add_subscriber("!r:x", "5521900000001", "manual") is True
    assert store.add_subscriber("!r:x", "5521900000001", "manual") is False
    store.add_subscriber("!r:x", "5521900000002", "csv")
    assert store.active_subscribers("!r:x") == ["5521900000001", "5521900000002"]

    assert store.remove_subscriber("!r:x", "5521900000001") is True
    assert store.active_subscribers("!r:x") == ["5521900000002"]
    # re-subscribing reactivates
    store.add_subscriber("!r:x", "5521900000001", "manual")
    assert len(store.active_subscribers("!r:x")) == 2

    assert store.unlink_room("!r:x") is True
    assert store.get_link("!r:x") is None


def test_opt_out_everywhere(store):
    store.add_subscriber("!a:x", "5521900000001", "m")
    store.add_subscriber("!b:x", "5521900000001", "m")
    assert store.opt_out_everywhere("5521900000001") == 2
    assert store.active_subscribers("!a:x") == [] and store.active_subscribers("!b:x") == []


def test_duplicate_alert_is_ignored(store):
    assert store.create_alert("!r:x", "$ev1", "@a:x", "oi") is not None
    assert store.create_alert("!r:x", "$ev1", "@a:x", "oi") is None


def test_status_never_goes_backwards(store):
    alert = store.create_alert("!r:x", "$ev", "@a:x", "oi")
    d = store.create_delivery(alert, "5521900000001")
    store.mark_accepted(d, "wamid.1")
    assert store.apply_status("wamid.1", "sent", 100, {}) is True
    assert store.apply_status("wamid.1", "read", 300, {}) is True
    assert store.apply_status("wamid.1", "delivered", 200, {}) is True  # out of order
    row = store.db.execute("SELECT * FROM delivery WHERE id=?", (d,)).fetchone()
    assert row["status"] == "read"
    assert (row["sent_at"], row["delivered_at"], row["read_at"]) == (100, 300, 300)
    assert store.delivery_summary(alert) == {"read": 1}


def test_unknown_wamid_is_recorded_but_not_matched(store):
    assert store.apply_status("wamid.zzz", "delivered", 1, {"a": 1}) is False
    assert store.db.execute("SELECT COUNT(*) FROM status_event").fetchone()[0] == 1


def test_failures(store):
    alert = store.create_alert("!r:x", "$ev", "@a:x", "oi")
    d1 = store.create_delivery(alert, "5521900000001")
    d2 = store.create_delivery(alert, "5521900000002")
    store.mark_failed(d1, 131026, "Message undeliverable")
    store.mark_accepted(d2, "wamid.2")
    store.apply_status("wamid.2", "failed", 5, {}, 130497, "restricted country")
    fails = store.failures(alert)
    assert {f["error_code"] for f in fails} == {131026, 130497}
    assert store.last_alert("!r:x")["id"] == alert
