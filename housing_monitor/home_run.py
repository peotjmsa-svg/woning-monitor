#!/usr/bin/env python3
"""
Controle thuis: draait via Windows Taakplanner (of later een Raspberry Pi) elk kwartier.

Vanaf een thuisverbinding laat Cloudflare de advertenties wel door. Deze run:
  1. haalt de nieuwste stand van GitHub op (instellingen, wachtlijst uit de alert-mails)
  2. werkt de wachtlijst af en zoekt zelf op de sites uit settings.json
  3. leest van elke nieuwe kandidaat de volledige advertentie, filtert op studenten,
     garantsteller, woningdelers, woningruil (monitor.py) en laat Claude beoordelen
  4. mailt de woningen die passen, en zet alles wat hij gedaan heeft in home_seen.json
     en log_home.json, die terug naar GitHub gaan

Sites van het type "builtin" (Pararius, Huurwoningen) hebben vaste code. Voor "generic"
sites zoekt hij op de zoekpagina links met `link_contains` erin, en haalt Claude de
gegevens uit de advertentie en beoordeelt die in één aanroep.

Gebruik:
  python housing_monitor/home_run.py            # normale run
  python housing_monitor/home_run.py --dry-run  # niets mailen, opslaan of pushen
"""
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bs4 import BeautifulSoup     # noqa: E402

import check_alerts as A          # noqa: E402  wachtlijst, sleutels, Claude, mail, logboek
import config as CONFIG           # noqa: E402
import monitor as M               # noqa: E402  scraper en filters op de advertentietekst

MAX_DESCRIPTION_CHARS = 12_000    # advertentieteksten zijn ±3.000 tekens; dit is ruim
MAX_PAGE_CHARS = 15_000           # paginatekst van een algemene site voor Claude
MAX_GENERIC_PER_SITE = 10         # nieuwe advertenties per algemene site per run (Claude-aanroepen)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # geen consolevenster vanuit Taakplanner
log = A.log

GENERIC_TOOL = {
    "name": "read_listing",
    "description": "Lees deze advertentiepagina uit en beoordeel de woning.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_listing": {"type": "boolean", "description": "false als dit geen losse huuradvertentie is"},
            "address": {"type": "string", "description": "Straat (+ huisnummer indien vermeld)"},
            "postcode": {"type": ["string", "null"], "description": "Bv. '1054 MD', null als onbekend"},
            "price_eur": {"type": ["integer", "null"], "description": "Huurprijs per maand in euro"},
            "rooms": {"type": ["integer", "null"], "description": "Totaal aantal kamers"},
            "bedrooms": {"type": ["integer", "null"]},
            "size_m2": {"type": ["integer", "null"]},
            "verdict": {"type": "string", "enum": ["fit", "geen fit", "twijfel"]},
            "reason": {"type": "string", "description": "1-2 zinnen onderbouwing, in het Nederlands"},
            "risks": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["is_listing", "address", "postcode", "price_eur", "rooms", "verdict", "reason", "risks"],
    },
}


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          creationflags=NO_WINDOW)


def fetch_final(session, url):
    """Als M.fetch, maar geeft ook de URL na doorsturen (de alert-links zijn tracking-links)."""
    r = session.get(url, headers=M.HEADERS, timeout=30)
    head = r.text[:3000].lower()
    if r.status_code in (403, 429) or "just a moment" in head or "cf-chl" in head:
        raise M.Blocked(f"{urlparse(str(r.url)).netloc} gaf {r.status_code}")
    r.raise_for_status()
    return r.text, str(r.url)


def url_key(url):
    p = urlparse(url)
    return f"url:{p.netloc.lower()}{p.path.rstrip('/').lower()}"


def detail_keys(detail):
    """Sleutels in hetzelfde formaat als de GitHub-kant: listing-id, postcode + prijs."""
    listing = {"address": detail.get("postcode") or "", "price_eur": detail.get("price"),
               "url": detail.get("url")}
    return [k for k in (A.listing_id_from_url(detail.get("url") or ""), A.postcode_key(listing),
                        url_key(detail["url"]) if detail.get("url") else None) if k]


def as_listing(detail):
    """Detailgegevens in het formaat van check_alerts (voor Claude, mail en logboek)."""
    return {
        "address": f"{detail['name']}, {detail.get('postcode') or ''} ({detail.get('area') or '?'})",
        "city": "Amsterdam",
        "price_eur": detail.get("price"),
        "rooms": detail.get("rooms"),
        "bedrooms": detail.get("bedrooms"),
        "size_m2": detail.get("size"),
        "income_requirement_text": None,
        "url": detail["url"].split("?")[0],
    }


def page_text(html):
    """Leesbare tekst van een pagina. Sommige sites kappen de beschrijving zichtbaar af en
    zetten de volledige tekst alleen in de gestructureerde data (JSON-LD); die komt erachter."""
    soup = BeautifulSoup(html, "html.parser")
    ld = M.jsonld_description(soup)
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "form"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    if ld and ld[:200] not in text:
        text += "\n\nVolledige beschrijving (uit de gestructureerde data van de pagina):\n" + ld
    return text


def generic_links(html, base_url, contains):
    """Advertentielinks op een zoekpagina van een algemene site: links op dezelfde site
    waarin `contains` staat, met daarna nog iets in het pad (een id of straatnaam).
    Zo vallen categoriepagina's als /appartement/huren/amsterdam/ af."""
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(base_url).netloc
    search = url_key(base_url)
    out = []
    for a in soup.find_all("a", href=True):
        p = urlparse(urljoin(base_url, a["href"]))
        i = p.path.find(contains)
        if p.netloc != host or i < 0 or not p.path[i + len(contains):].strip("/"):
            continue
        url = f"{p.scheme}://{p.netloc}{p.path}"
        if url_key(url) != search and url not in out:
            out.append(url)
    return out


def judge_builtin(client, detail):
    """Filters op de volledige advertentie + oordeel van Claude.
    Geeft (resultaat of None, fase, uitleg)."""
    ok, reasons, warnings = M.evaluate(detail)
    if not ok:
        return None, "filters", ", ".join(reasons)
    desc = detail["description"]
    if len(desc) > MAX_DESCRIPTION_CHARS:
        warnings.append(f"lange advertentie, alleen de eerste {MAX_DESCRIPTION_CHARS} tekens beoordeeld")
        desc = desc[:MAX_DESCRIPTION_CHARS]
    listing = as_listing(detail)
    listing["other_details"] = "Volledige advertentietekst:\n" + desc
    verdict = A.judge_listing(client, listing)
    if verdict["verdict"] == "geen fit":
        return None, "Claude (advertentie)", verdict["reason"]
    return {"listing": listing, "verdict": verdict, "warnings": warnings}, "Claude (advertentie)", verdict["reason"]


def read_generic(client, html, url):
    """Algemene site: Claude haalt de gegevens eruit en oordeelt in één aanroep.
    Geeft (detail-dict of None als het geen advertentie is, oordeel, waarschuwingen)."""
    text, warnings = page_text(html), []
    if len(text) > MAX_PAGE_CHARS:
        warnings.append(f"lange pagina, alleen de eerste {MAX_PAGE_CHARS} tekens gelezen")
        text = text[:MAX_PAGE_CHARS]
    system = (A.judge_system() + "\n\nJe krijgt de tekst van een advertentiepagina. Haal de gegevens "
              "eruit (verzin niets: onbekend is null) en geef je oordeel. Negeer menu's, voetteksten "
              "en andere woningen die op de pagina als suggestie staan. is_listing is alleen false "
              "als de pagina niet over één specifieke woning gaat (overzicht, categorie, foutpagina); "
              "een woningruil of dure woning is wél een advertentie, die wijs je af.")
    data = A.call_tool(client, GENERIC_TOOL, system, f"URL: {url}\n\n{text}", max_tokens=1500)
    if not data.get("is_listing"):
        return None, None, warnings
    pc = re.search(r"(1\d{3})\s?([A-Za-z]{2})", data.get("postcode") or "")
    detail = {
        "name": data.get("address") or url, "url": url,
        "price": data.get("price_eur"), "rooms": data.get("rooms"),
        "bedrooms": data.get("bedrooms"), "size": data.get("size_m2"),
        "postcode": f"{pc.group(1)} {pc.group(2).upper()}" if pc else None,
        "description": text,
    }
    verdict = {k: data[k] for k in ("verdict", "reason", "risks")}
    return detail, verdict, warnings


def run(dry_run=False):
    if not dry_run:
        pulled = git("pull", "--rebase", "--autostash", "-q")
        if pulled.returncode:
            log(f"git pull mislukt, ga door met de lokale stand: {pulled.stderr.strip()[:200]}")
    CONFIG.reload()                    # instellingen kunnen via de webapp veranderd zijn
    if CONFIG.SETTINGS_ERROR:
        log(f"LET OP: {CONFIG.SETTINGS_ERROR}")

    github_state = A.load_state()
    queue = A.load_json(A.QUEUE_FILE, {"items": []})
    home = A.load_json(A.HOME_SEEN_FILE, {"listings": {}})
    now = datetime.now(timezone.utc).isoformat()
    done_elsewhere = set(github_state["listings"])     # GitHub heeft ze al afgewezen of gemaild
    client = A.make_client()
    session = M.new_session()
    matches, events, sites_status, n_checked = [], [], {}, 0

    def seen(keys):
        return any(k in done_elsewhere or k in home["listings"] for k in keys)

    def mark(keys, result):
        for k in keys:
            home["listings"][k] = {"t": now, "result": result}

    def record(source, site, keys, listing, result, stage, reason, match=None):
        mark(keys, result)
        events.append(A.log_event(source, site, listing, result, stage, reason))
        if match:
            matches.append(match)
        log(f"  {'✓' if match else '✗'} {listing.get('address')} {A.fmt_price(listing.get('price_eur'))}: {reason}")

    def handle_builtin(source, site, detail, extra_keys=()):
        nonlocal n_checked
        keys = detail_keys(detail) + list(extra_keys)
        if seen(keys):
            mark(keys, "al gezien")
            return
        n_checked += 1
        result, stage, why = judge_builtin(client, detail)
        record(source, site, keys, as_listing(detail), "gemaild" if result else "afgewezen", stage, why, result)

    # 1. Wachtlijst van GitHub: woningen uit de alert-mails
    todo = [it for it in queue["items"] if not any(k in home["listings"] for k in it["keys"])]
    log(f"Wachtlijst van GitHub: {len(todo)} te controleren")
    for it in todo:
        url = it["listing"].get("url")
        if not url:
            continue
        try:
            html, final_url = fetch_final(session, url)     # volgt de doorstuurlink uit de mail
            handle_builtin("wachtlijst", it.get("site", "Mail"), M.parse_detail(html, final_url), it["keys"])
        except M.Blocked as e:
            log(f"Geblokkeerd: {e}")
            break
        except (M.RequestException, A.anthropic.APIError, RuntimeError) as e:
            log(f"  Fout bij {it['listing'].get('address')}: {e} (GitHub mailt hem zo nodig ongecontroleerd)")
        M.polite_sleep()

    # 2. Zelf zoeken op de sites uit de instellingen
    budget = CONFIG.MAX_DETAIL_FETCHES_PER_RUN
    for site in CONFIG.SITES:
        name, generic = site["name"], site["type"] == "generic"
        try:
            html = M.fetch(session, site["search_url"])
            links = (generic_links(html, site["search_url"], site["link_contains"]) if generic
                     else [u for _, u in M.extract_listing_links(html, site["search_url"])])
        except (M.Blocked, M.RequestException) as e:
            log(f"{name}: zoekpagina mislukt: {e}")
            sites_status[name] = {"t": now, "ok": False, "found": 0, "error": str(e)[:200]}
            M.polite_sleep()
            continue
        new = [u for u in links if not seen([url_key(u)] + ([A.listing_id_from_url(u)] if not generic else []))]
        sites_status[name] = {"t": now, "ok": bool(links), "found": len(links), "new": len(new),
                              "error": None if links else "0 advertentielinks gevonden (klopt link_contains / de URL?)"}
        log(f"{name}: {len(links)} advertenties op de zoekpagina, {len(new)} nieuw")
        M.polite_sleep()

        for url in new[:MAX_GENERIC_PER_SITE if generic else budget]:
            if budget <= 0:
                break
            budget -= 1
            try:
                html, final_url = fetch_final(session, url)
                if not generic:
                    lid = A.listing_id_from_url(url)
                    handle_builtin("scraper", name, M.parse_detail(html, final_url), [lid, url_key(url)] if lid else [url_key(url)])
                else:
                    detail, verdict, warnings = read_generic(client, html, final_url)
                    keys = [url_key(url), url_key(final_url)]
                    if detail is None:
                        mark(keys, "geen advertentie")
                        continue
                    keys += detail_keys(detail)
                    if seen(keys):
                        mark(keys, "al gezien")
                        continue
                    n_checked += 1
                    # Alleen de harde regels (prijs, kamers, gebied): de tekstregels zouden op
                    # menu's en voetteksten afgaan; die afweging maakt Claude hierboven al.
                    ok, reasons, more = M.evaluate(dict(detail, description=""))
                    detail["area"] = M.classify_area(detail["postcode"])   # evaluate zette het op de kopie
                    listing = as_listing(detail)
                    if not ok:
                        record("scraper", name, keys, listing, "afgewezen", "filters", ", ".join(reasons))
                    elif verdict["verdict"] == "geen fit":
                        record("scraper", name, keys, listing, "afgewezen", "Claude (advertentie)", verdict["reason"])
                    else:
                        record("scraper", name, keys, listing, "gemaild", "Claude (advertentie)", verdict["reason"],
                               {"listing": listing, "verdict": verdict, "warnings": warnings + more})
            except M.Blocked as e:
                log(f"  {name} blokkeert de advertentie: {e}")
                sites_status[name].update(ok=False, error=f"advertentie geblokkeerd: {e}"[:200])
                break
            except (M.RequestException, A.anthropic.APIError, RuntimeError) as e:
                log(f"  Fout bij {url}: {e} (volgende run opnieuw)")
            M.polite_sleep()

    log(f"Resultaat: {n_checked} advertentie(s) gecontroleerd, {len(matches)} passend")
    if dry_run:
        if matches:
            log("[DRY RUN] zou mailen: " + A.build_summary(matches, checked=True)[0])
        return

    # Eerst mailen, dan opslaan en pushen (mislukt het mailen, dan volgende run opnieuw)
    if matches:
        A.send_email(*A.build_summary(matches, checked=True))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=A.STATE_TTL_DAYS)).isoformat()
    home["listings"] = {k: v for k, v in home["listings"].items() if v["t"] >= cutoff}
    A.save_json(A.HOME_SEEN_FILE, home)
    A.save_log(A.LOG_HOME_FILE, events, sites_status)

    files = [str(p.relative_to(ROOT)) for p in (A.HOME_SEEN_FILE, A.LOG_HOME_FILE)]
    git("add", *files)
    if git("diff", "--cached", "--quiet").returncode:
        git("commit", "-q", "-m", "home_seen/log update", "--", *files)
        for _ in range(3):                 # GitHub of de webapp kan net tegelijk iets gepusht hebben
            git("pull", "--rebase", "--autostash", "-q")
            if git("push", "-q").returncode == 0:
                log("Resultaten naar GitHub gepusht")
                break
        else:
            log("Push naar GitHub mislukt; volgende run opnieuw (GitHub mailt dan mogelijk ongecontroleerd)")


if __name__ == "__main__":
    windowless = M.log_to_file_if_windowless()
    M.load_env_file()
    dry = "--dry-run" in sys.argv
    if not dry and not all(os.environ.get(k) for k in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "DESTINATION_EMAIL")):
        # Anders worden woningen als gecontroleerd gemarkeerd zonder dat je ze gemaild krijgt
        log("Overgeslagen: vul eerst GMAIL_ADDRESS, GMAIL_APP_PASSWORD en DESTINATION_EMAIL in .env in")
    else:
        run(dry_run=dry)
