import pytest

from matrix_wacloud.config import ConfigError, parse_config
from matrix_wacloud.util import InvalidPhone, mask_phone, normalize_phone, sanitize_param


@pytest.mark.parametrize("raw,expected", [
    ("(21) 99999-1234", "5521999991234"),
    ("21999991234", "5521999991234"),
    ("2133334444", "552133334444"),          # fixo, 10 dígitos
    ("+55 21 99999-1234", "5521999991234"),
    ("0055 21 99999 1234", "5521999991234"),
    ("+1 555 147 7651", "15551477651"),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "123", "abc", "+1234567890123456"])
def test_normalize_phone_invalid(raw):
    with pytest.raises(InvalidPhone):
        normalize_phone(raw)


def test_mask_phone():
    assert mask_phone("5521999991234") == "5521*****1234"


def test_sanitize_param_removes_newlines_and_spaces():
    assert sanitize_param("Sem aula hoje\n\nFalta de água\tno campus     Urca") == \
        "Sem aula hoje | Falta de água no campus Urca"


def test_sanitize_param_truncates_and_empty():
    assert sanitize_param("   ") == "-"
    out = sanitize_param("x" * 2000, max_len=50)
    assert len(out) == 50 and out.endswith("…")


def _raw(**over):
    raw = {
        "homeserver": {"address": "http://hs:8008", "domain": "${TEST_DOMAIN}"},
        "appservice": {"id": "wacloud", "bot_localpart": "wacloud", "as_token": "a", "hs_token": "h",
                       "address": "http://bridge:29330"},
        "whatsapp": {"phone_number_id": "1", "access_token": "t", "verify_token": "v"},
        "admins": ["@admin:${TEST_DOMAIN}"],
    }
    raw.update(over)
    return raw


def test_config_env_expansion(monkeypatch):
    monkeypatch.setenv("TEST_DOMAIN", "example.org")
    cfg = parse_config(_raw())
    assert cfg.homeserver.domain == "example.org"
    assert cfg.admins == ["@admin:example.org"]
    assert cfg.bot_mxid == "@wacloud:example.org"
    assert cfg.whatsapp.params_for("hello_world") == []
    assert cfg.whatsapp.params_for("aviso") == ["label", "text"]


def test_config_missing_env(monkeypatch):
    monkeypatch.delenv("TEST_DOMAIN", raising=False)
    with pytest.raises(ConfigError, match="TEST_DOMAIN"):
        parse_config(_raw())


def test_config_default_value(monkeypatch):
    monkeypatch.setenv("TEST_DOMAIN", "example.org")
    raw = _raw()
    raw["database"] = "${NOT_SET_XYZ:-/tmp/x.db}"
    assert parse_config(raw).database == "/tmp/x.db"


def test_config_rejects_unknown_param_token(monkeypatch):
    monkeypatch.setenv("TEST_DOMAIN", "example.org")
    raw = _raw()
    raw["whatsapp"]["template_params"] = {"t": ["label", "bogus"]}
    with pytest.raises(ConfigError, match="bogus"):
        parse_config(raw)


def test_config_requires_admins(monkeypatch):
    monkeypatch.setenv("TEST_DOMAIN", "example.org")
    with pytest.raises(ConfigError, match="admins"):
        parse_config(_raw(admins=[]))
