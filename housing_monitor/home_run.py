#!/usr/bin/env python3
"""
Controle thuis: draait via Windows Taakplanner (of later een Raspberry Pi) elk kwartier.

Vanaf een thuisverbinding laat Cloudflare de advertenties wel door. Deze run:
  1. haalt de wachtlijst van GitHub op (woningen uit de alert-mails, queue.json)
  2. zoekt zelf ook op Pararius en Huurwoningen (de scraper uit monitor.py)
  3. leest van elke nieuwe kandidaat de volledige advertentie, filtert op studenten,
     garantsteller, woningdelers, woningruil (monitor.py) en laat Claude beoordelen
  4. mailt de woningen die passen, en zet alles wat hij gedaan heeft in home_seen.json,
     dat terug naar GitHub gaat zodat GitHub niets dubbel mailt

Gebruik:
  python housing_monitor/home_run.py            # normale run
  python housing_monitor/home_run.py --dry-run  # niets mailen, opslaan of pushen
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_alerts as A          # noqa: E402  wachtlijst, sleutels, Claude, mail
import config as CONFIG           # noqa: E402
import monitor as M               # noqa: E402  scraper en filters op de advertentietekst

MAX_DESCRIPTION_CHARS = 12_000    # advertentieteksten zijn ±3.000 tekens; dit is ruim
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # geen consolevenster vanuit Taakplanner
log = A.log


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          creationflags=NO_WINDOW)


def fetch_final(session, url):
    """Als M.fetch, maar geeft ook de URL na doorsturen (de alert-links zijn tracking-links)."""
    r = session.get(url, headers=M.HEADERS, timeout=30)
    head = r.text[:3000].lower()
    if r.status_code in (403, 429) or "just a moment" in head or "cf-chl" in head:
        raise M.Blocked(f"{r.url} gaf {r.status_code}")
    r.raise_for_status()
    return r.text, str(r.url)


def detail_keys(detail):
    """Sleutels in hetzelfde formaat als de GitHub-kant: listing-id, postcode + prijs."""
    listing = {"address": detail.get("postcode") or "", "price_eur": detail.get("price"),
               "url": detail.get("url")}
    return [k for k in (A.listing_id_from_url(detail.get("url") or ""), A.postcode_key(listing)) if k]


def check_detail(client, detail):
    """Filters op de volledige advertentie + oordeel van Claude.
    Geeft (resultaat of None, korte uitleg)."""
    ok, reasons, warnings = M.evaluate(detail)
    if not ok:
        return None, ", ".join(reasons)
    desc = detail["description"]
    if len(desc) > MAX_DESCRIPTION_CHARS:
        warnings.append(f"lange advertentie, alleen de eerste {MAX_DESCRIPTION_CHARS} tekens beoordeeld")
        desc = desc[:MAX_DESCRIPTION_CHARS]
    listing = {
        "address": f"{detail['name']}, {detail.get('postcode') or ''} ({detail.get('area') or '?'})",
        "city": "Amsterdam",
        "price_eur": detail.get("price"),
        "rooms": detail.get("rooms"),
        "bedrooms": detail.get("bedrooms"),
        "size_m2": detail.get("size"),
        "income_requirement_text": None,
        "other_details": "Volledige advertentietekst:\n" + desc,
        "url": detail["url"].split("?")[0],
    }
    verdict = A.judge_listing(client, listing)
    if verdict["verdict"] == "geen fit":
        return None, verdict["reason"]
    return {"listing": listing, "verdict": verdict, "warnings": warnings}, verdict["verdict"]


def run(dry_run=False):
    if not dry_run:
        pulled = git("pull", "--rebase", "--autostash", "-q")
        if pulled.returncode:
            log(f"git pull mislukt, ga door met de lokale stand: {pulled.stderr.strip()[:200]}")

    github_state = A.load_state()
    queue = A.load_json(A.QUEUE_FILE, {"items": []})
    home = A.load_json(A.HOME_SEEN_FILE, {"listings": {}})
    now = datetime.now(timezone.utc).isoformat()
    done_elsewhere = set(github_state["listings"])     # GitHub heeft ze al afgewezen of gemaild
    client = A.make_client()
    session = M.new_session()
    matches, n_checked, blocked = [], 0, False

    def mark(keys, result):
        for k in keys:
            home["listings"][k] = {"t": now, "result": result}

    def handle(detail, extra_keys=()):
        nonlocal n_checked
        keys = detail_keys(detail) + list(extra_keys)
        if any(k in done_elsewhere or k in home["listings"] for k in keys):
            mark(keys, "al gezien")
            return
        n_checked += 1
        result, why = check_detail(client, detail)
        log(f"  {'✓' if result else '✗'} {detail['name']} {A.fmt_price(detail.get('price'))}: {why}")
        mark(keys, "match" if result else "afgewezen")
        if result:
            matches.append(result)

    # 1. Wachtlijst van GitHub: woningen uit de alert-mails
    todo = [it for it in queue["items"] if not any(k in home["listings"] for k in it["keys"])]
    log(f"Wachtlijst van GitHub: {len(todo)} te controleren")
    for it in todo:
        url = it["listing"].get("url")
        if not url:
            continue
        try:
            html, final_url = fetch_final(session, url)   # volgt de doorstuurlink uit de mail
            handle(M.parse_detail(html, final_url), extra_keys=it["keys"])
        except M.Blocked as e:
            log(f"Geblokkeerd: {e}")
            blocked = True
            break
        except (M.RequestException, A.anthropic.APIError, RuntimeError) as e:
            log(f"  Fout bij {it['listing'].get('address')}: {e} (GitHub mailt hem zo nodig ongecontroleerd)")
        M.polite_sleep()

    # 2. Zelf zoeken op de sites
    candidates = {}
    if not blocked:
        for url in CONFIG.SEARCH_URLS:
            try:
                for lid, lurl in M.extract_listing_links(M.fetch(session, url), url):
                    if lid not in home["listings"] and lid not in done_elsewhere:
                        candidates.setdefault(lid, lurl)
            except M.Blocked as e:
                log(f"Geblokkeerd: {e}")
                blocked = True
                break
            except M.RequestException as e:
                log(f"Fout bij {url}: {e}")
            M.polite_sleep()
    log(f"Scraper: {len(candidates)} nieuwe woningen op de zoekpagina's")
    for lid, lurl in list(candidates.items())[:CONFIG.MAX_DETAIL_FETCHES_PER_RUN]:
        try:
            detail = M.parse_detail(M.fetch(session, lurl), lurl)
        except M.Blocked as e:
            log(f"Geblokkeerd: {e}")
            break
        except M.RequestException as e:
            log(f"  Fout bij {lurl}: {e} (volgende run opnieuw)")
            continue
        try:
            handle(detail, extra_keys=[lid])
        except (A.anthropic.APIError, RuntimeError) as e:
            log(f"  Claude-fout bij {detail['name']}: {e} (volgende run opnieuw)")
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

    rel = str(A.HOME_SEEN_FILE.relative_to(ROOT))
    git("add", rel)
    if git("diff", "--cached", "--quiet").returncode:
        git("commit", "-q", "-m", "home_seen update", "--", rel)
        for _ in range(3):                 # GitHub kan net tegelijk iets gepusht hebben
            git("pull", "--rebase", "--autostash", "-q")
            if git("push", "-q").returncode == 0:
                log("home_seen.json naar GitHub gepusht")
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
