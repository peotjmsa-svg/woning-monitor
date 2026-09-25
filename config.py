"""Instellingen voor de woning-monitor.

De waarden komen uit housing_monitor/settings.json, dat je via de webapp aanpast.
Hieronder staan de standaardwaarden; die gelden voor alles wat in settings.json
ontbreekt, en voor alles als dat bestand kapot is (SETTINGS_ERROR zegt dan waarom).
Na het binnenhalen van een nieuwe versie: config.reload().
"""
import copy
import json
from pathlib import Path

SETTINGS_FILE = Path(__file__).with_name("housing_monitor") / "settings.json"

DEFAULTS = {
    "enabled": True,                    # false = de hele monitor slaat elke run over (kill switch)
    "max_price": 3000,                  # euro per maand
    "min_rooms": 4,                     # totaal aantal kamers, incl. woonkamer
    # Gebieden binnen de ring (A10), op basis van de eerste 4 cijfers van de postcode.
    # Een benadering: aan de randen kan een postcode net buiten de ring vallen.
    "areas": {
        "Centrum": [[1011, 1018]],
        "West": [[1051, 1059]],
        "Zuid": [[1071, 1079]],
        "Oost": [[1019, 1019], [1091, 1098]],
    },
    # Per gebied een max prijs per kamer ("alleen bij een goede deal")
    "area_max_price_per_room": {"Oost": 625},
    "include_unknown_postcode": True,   # woningen zonder gevonden postcode wel doorsturen
    "reject": {
        "no_students": True,            # "niet verhuurd aan studenten"
        "no_guarantor": True,           # "garantstelling niet geaccepteerd"
        "no_sharing": True,             # "geen woningdelers"
        "swap": True,                   # woningruil
    },
    "income_reject_above": None,        # afwijzen boven deze inkomenseis per jaar; None = alleen tonen
    # Letterlijk wat Claude als zoekcriteria krijgt
    "criteria_text": (
        "- Locatie: binnen de Amsterdamse A10-ring. Oost mag, maar alleen bij een duidelijk goede deal.\n"
        "- Budget: max €3000/maand\n"
        "- Aantal kamers: 4 of meer\n"
        "- Oppervlakte: maakt niet uit\n"
        "- We zoeken met 3 personen (2 kan ook, dan wel een extra kamer naast de woonkamer)\n"
        "- We zijn studenten: listings met een hoge inkomenseis vallen af\n"
        "- Let op: listings die een \"moet studeren in Amsterdam\"-eis stellen zijn problematisch — "
        "een van ons studeert een niet-Amsterdamse master, dus flag dat expliciet als risico i.p.v. "
        "het gewoon af te keuren"
    ),
    "fallback_minutes": 60,             # zo lang wacht GitHub op controle door de pc thuis
    "alert_senders": ["pararius", "huurwoningen"],   # afzenders van alert-mails (deel van het adres)
    # Zoekpagina's voor de pc thuis. Alleen pagina 1 wordt gelezen, dus sorteer op nieuwste.
    # type "builtin": vaste code voor Pararius/Huurwoningen; "generic": elke andere site,
    # Claude leest de advertentie (link_contains: tekst die in elke advertentielink staat);
    # "mijndak": amsterdam.mijndak.nl met je account (inlog in .env op de pc);
    # "vesteda": vesteda.com via hun zoek-API (search_url = hun zoekpagina, alleen ter info).
    # Een * in link_contains matcht het hele pad (bv. "/wonen/object/*-amsterdam/"), en
    # "render": true laadt de zoekpagina in een browser voor sites die JavaScript nodig hebben.
    "sites": [
        {"name": "Pararius", "type": "builtin", "enabled": True,
         "search_url": "https://www.pararius.nl/huurwoningen/amsterdam/0-3000/4-aantalkamers"},
        {"name": "Huurwoningen", "type": "builtin", "enabled": True,
         "search_url": "https://www.huurwoningen.nl/in/amsterdam/?price=0-3000&rooms=4&sort=published_at&direction=desc"},
    ],
    "max_detail_fetches_per_run": 25,   # advertenties per run op de pc (rest volgt de volgende run)
    "request_delay": [2.0, 4.0],        # pauze tussen requests in seconden (min, max)
}

SETTINGS_ERROR = None


def _merge(defaults, override):
    out = copy.deepcopy(defaults)
    for key, value in (override or {}).items():
        if key not in defaults:
            continue                    # onbekende sleutels negeren
        if isinstance(defaults[key], dict) and isinstance(value, dict) and key == "reject":
            out[key].update({k: bool(v) for k, v in value.items() if k in defaults[key]})
        else:
            out[key] = value
    return out


def _validate(s):
    assert isinstance(s["enabled"], bool), "enabled moet true of false zijn"
    assert isinstance(s["max_price"], (int, float)) and s["max_price"] > 0, "max_price moet een getal > 0 zijn"
    assert isinstance(s["min_rooms"], int) and s["min_rooms"] >= 1, "min_rooms moet een geheel getal >= 1 zijn"
    for area, ranges in s["areas"].items():
        for r in ranges:
            assert len(r) == 2 and 1000 <= r[0] <= r[1] <= 1199, f"ongeldige postcodes bij {area}: {r}"
    for area, v in s["area_max_price_per_room"].items():
        assert isinstance(v, (int, float)) and v > 0, f"ongeldige prijs per kamer bij {area}"
    assert s["income_reject_above"] is None or s["income_reject_above"] > 0, "ongeldige inkomensgrens"
    assert isinstance(s["criteria_text"], str) and s["criteria_text"].strip(), "criteria_text is leeg"
    assert isinstance(s["fallback_minutes"], int) and s["fallback_minutes"] >= 0, "ongeldige fallback_minutes"
    assert all(isinstance(x, str) and x.strip() for x in s["alert_senders"]), "ongeldige alert_senders"
    for site in s["sites"]:
        assert site.get("name") and site.get("search_url", "").startswith("http"), f"ongeldige site: {site}"
        assert site.get("type") in ("builtin", "generic", "mijndak", "vesteda"), f"onbekend sitetype: {site.get('type')}"
        if site["type"] == "generic":
            assert site.get("link_contains"), f"{site['name']}: link_contains ontbreekt"
    assert isinstance(s["max_detail_fetches_per_run"], int) and s["max_detail_fetches_per_run"] > 0
    assert len(s["request_delay"]) == 2 and 0 <= s["request_delay"][0] <= s["request_delay"][1]


def _apply(s):
    g = globals()
    g["SETTINGS"] = s
    g["ENABLED"] = bool(s["enabled"])
    g["MAX_PRICE"] = s["max_price"]
    g["MIN_ROOMS"] = s["min_rooms"]
    g["AREAS"] = {a: [tuple(r) for r in ranges] for a, ranges in s["areas"].items()}
    g["AREA_MAX_PRICE_PER_ROOM"] = dict(s["area_max_price_per_room"])
    g["INCLUDE_UNKNOWN_POSTCODE"] = bool(s["include_unknown_postcode"])
    g["REJECT_NO_STUDENTS"] = s["reject"]["no_students"]
    g["REJECT_NO_GUARANTOR"] = s["reject"]["no_guarantor"]
    g["REJECT_NO_SHARING"] = s["reject"]["no_sharing"]
    g["REJECT_SWAP"] = s["reject"]["swap"]
    g["INCOME_REJECT_ABOVE"] = s["income_reject_above"]
    g["CRITERIA_TEXT"] = s["criteria_text"].strip()
    g["FALLBACK_MINUTES"] = s["fallback_minutes"]
    g["ALERT_SENDERS"] = [x.strip().lower() for x in s["alert_senders"]]
    g["SITES"] = [dict(site) for site in s["sites"] if site.get("enabled", True)]
    g["SEARCH_URLS"] = [site["search_url"] for site in g["SITES"] if site["type"] == "builtin"]
    g["MAX_DETAIL_FETCHES_PER_RUN"] = s["max_detail_fetches_per_run"]
    g["REQUEST_DELAY"] = tuple(s["request_delay"])


def reload():
    """Leest settings.json opnieuw. Bij een fout gelden de standaardwaarden."""
    global SETTINGS_ERROR
    SETTINGS_ERROR = None
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.exists() else {}
        s = _merge(DEFAULTS, raw)
        _validate(s)
    except (ValueError, AssertionError, TypeError, KeyError) as e:
        SETTINGS_ERROR = f"settings.json niet gebruikt ({e}); standaardinstellingen actief"
        s = copy.deepcopy(DEFAULTS)
    _apply(s)


reload()
