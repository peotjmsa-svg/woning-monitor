"""Aanbod van amsterdam.mijndak.nl, met je eigen account (alleen op de pc thuis).

Mijndak toont woningen alleen aan ingelogde leden. Een onzichtbare browser (Playwright)
logt in met MIJNDAK_USERNAME en MIJNDAK_PASSWORD uit .env, opent het passende en het
niet-passende overzicht, en vangt de gegevens op die de site zelf ophaalt. De sessie
wordt bewaard in .mijndak_session.json, zodat hij niet elk kwartier opnieuw inlogt.

Installatie (eenmalig, in de venv):  pip install playwright && python -m playwright install chromium
"""
import os
from pathlib import Path

BASE = "https://amsterdam.mijndak.nl"
SESSION_FILE = Path(__file__).resolve().parent.parent / ".mijndak_session.json"
LISTS = {
    "passend": ("/WoningOverzicht", "DataActionHaalPassendAanbod"),
    "niet passend": ("/WoningOverzichtNietPassend", "DataActionHaalNietPassendAanbod"),
}
HOMES = {"Woonruimte", "Woning"}          # geen parkeerplaatsen, bergingen of bedrijfsruimte
MODEL_UITLEG = {
    "Aanbodmodel": "sociale huur, volgorde op inschrijfduur",
    "Lotingmodel": "loting",
    "Vrije sector": "vrije sector",
}


class LoginFailed(Exception):
    pass


def _login(page):
    user, pw = os.environ.get("MIJNDAK_USERNAME"), os.environ.get("MIJNDAK_PASSWORD")
    if not (user and pw):
        raise LoginFailed("MIJNDAK_USERNAME/MIJNDAK_PASSWORD ontbreken in .env")
    page.goto(BASE + "/Inloggen", wait_until="networkidle", timeout=60000)
    page.locator("input[type=email], input[type=text]").first.fill(user)
    page.locator("input[type=password]").first.fill(pw)
    page.get_by_role("button", name="Log in").first.click()
    page.wait_for_load_state("networkidle", timeout=60000)
    page.wait_for_timeout(2000)
    if "/Inloggen" in page.url:
        # Niet opnieuw proberen: te veel mislukte pogingen kunnen het account blokkeren
        raise LoginFailed("inloggen geweigerd (klopt het wachtwoord in .env?)")


def fetch_publications():
    """Alle woningpublicaties uit het passende en niet-passende overzicht.
    Geeft [(publicatie-dict, 'passend' | 'niet passend')]. Gooit LoginFailed of een
    Playwright-fout als het niet lukt."""
    from playwright.sync_api import sync_playwright

    captured = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            locale="nl-NL",
            storage_state=str(SESSION_FILE) if SESSION_FILE.exists() else None)
        page = ctx.new_page()

        def on_response(r):
            name = r.url.rsplit("/", 1)[-1]
            if name in {action for _, action in LISTS.values()}:
                try:
                    captured[name] = r.json()
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            out = []
            for label, (path, action) in LISTS.items():
                page.goto(BASE + path, wait_until="networkidle", timeout=60000)
                if "/Inloggen" in page.url:          # sessie verlopen
                    _login(page)
                    page.goto(BASE + path, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(3000)
                data = (captured.get(action) or {}).get("data") or {}
                if captured.get(action, {}).get("exception"):
                    raise RuntimeError(f"mijndak {label}: {captured[action]['exception'].get('message')}")
                out += [(pub, label) for pub in (data.get("PublicatieLijst") or {}).get("List", [])]
            ctx.storage_state(path=str(SESSION_FILE))
            return out
        finally:
            browser.close()


def _num(value):
    """Mijndak geeft getallen soms als tekst ("75.00"); 0 of onleesbaar = onbekend."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n or None


def to_detail(pub, label):
    """Publicatie in het formaat van monitor.parse_detail (voor filters, Claude en mail)."""
    e, a = pub.get("Eenheid") or {}, pub.get("Adres") or {}
    rent = _num(e.get("Brutohuur")) if e.get("BrutoHuurBekend") else None
    rent = rent or _num(e.get("NettoHuur"))
    rooms, size = _num(e.get("AantalKamers")), _num(e.get("WoonVertrekkenTotOpp")) or _num(e.get("TotaleOppervlakte"))
    pc = a.get("Postcode") or ""
    model = pub.get("PublicatieModel") or ""
    lines = [
        f"Aangeboden via mijndak ({MODEL_UITLEG.get(model, model or 'onbekend model')}).",
        f"Mijndak vindt deze woning {label} voor het account van de woningzoekende.",
        f"Doelgroep: {e.get('Doelgroep') or '?'}. Contract: {pub.get('ContractVorm') or '?'}.",
        f"Kale huur €{e.get('NettoHuur')}, totale huur €{e.get('Brutohuur')}.",
        f"Reageren kan tot {str(pub.get('EinddatumTijd') or '')[:10]}; "
        f"voorlopige positie {pub.get('VoorlopigePositie')}, {pub.get('AantalReactiesOpPublicatie')} reacties.",
        f"Eigenaar: {e.get('Eigenaar') or '?'}. Wijk: {a.get('Wijk') or '?'}, {a.get('Woonplaats') or '?'}.",
    ]
    return {
        "name": f"{a.get('Straatnaam', '')} {a.get('Huisnummer', '')}{a.get('HuisnummerToevoeging') or ''}".strip(),
        "url": f"{BASE}/HuisDetails?PublicatieId={pub['Id']}",
        "price": round(rent) if rent else None,
        "postcode": f"{pc[:4]} {pc[4:].strip()}" if len(pc) >= 6 else None,
        "rooms": int(rooms) if rooms else None,
        "bedrooms": None,
        "size": round(size) if size else None,
        "description": "\n".join(lines),
        "is_home": pub.get("EenheidSoort") in HOMES,
        "passend": label == "passend",
        "id": pub["Id"],
    }
