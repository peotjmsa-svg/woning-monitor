# Woning-monitor Amsterdam

Checkt elke 15 minuten Pararius en Huurwoningen.nl, leest de beschrijving van elke nieuwe woning en mailt alleen de woningen die passen.

## Wat hij filtert

- Max €3000 p/m, 4+ kamers
- Binnen de ring (Centrum, West, Zuid, Oost), op basis van postcode
- Oost alleen als het een goede deal is (max €625 per kamer)
- Afgewezen als de beschrijving zegt: geen studenten, geen garantsteller, geen woningdelers, of als het een woningruil is
- Waarschuwing (wel gemaild) bij: inkomenseis, max 2 personen/woningdelers, geen friends-contract, inkomen uit werk vereist
- Dezelfde woning op beide sites krijg je maar één keer (herkend aan postcode + prijs)

Alles is aan te passen in `config.py`. Onderaan elke mail staat wat er is weggefilterd en waarom, zodat je kan zien of het filter goed werkt.

## Installatie (±10 minuten, liefst op een laptop)

**1. Gmail app-wachtwoord maken**
- Zet tweestapsverificatie aan op je Google-account (vereist)
- Ga naar https://myaccount.google.com/apppasswords
- Maak een wachtwoord aan met naam "woning-monitor" en kopieer de 16 letters

**2. GitHub-repo maken**
- Maak een account op github.com en klik op **New repository**
- Naam bijv. `woning-monitor`. Kies **Public**: dan zijn de Actions-minuten onbeperkt gratis. Er staat niks gevoeligs in, je wachtwoord komt in Secrets.
- Klik **uploading an existing file** en sleep de hele map erin (inclusief de map `.github`). Tip: op een Mac zijn mappen met een punt verborgen, druk Cmd+Shift+. in Finder om ze te zien.

**3. Secrets instellen**
Repo → Settings → Secrets and variables → Actions → New repository secret:

| Naam | Waarde |
|---|---|
| `GMAIL_USER` | jouw gmail-adres |
| `GMAIL_APP_PASSWORD` | het app-wachtwoord van stap 1 |
| `EMAIL_TO` | ontvangers, komma-gescheiden (bijv. jij en je huisgenoten) |

**4. Starten**
Tab **Actions** → Woning monitor → **Run workflow**. De eerste run mailt alles wat nu online staat en aan je criteria voldoet, daarna alleen nog nieuwe woningen.

## Handig

- **Eén woning testen:** `python monitor.py --test <url>` laat zien wat de tool eruit haalt en of hij 'm zou doorsturen.
- **Filter aangepast?** Draai `python test_filters.py`: dat checkt de filters op zinnen uit echte advertenties.
- **Private repo?** Dan heb je 2000 gratis minuten per maand; zet de cron in `.github/workflows/monitor.yml` op `*/30`.
- **Problemen?** Als een site blokkeert, of een zoekpagina 3 runs op rij faalt of 0 woningen geeft (bijv. omdat de URL veranderd is), krijg je max 1x per dag een waarschuwingsmail.
- **Cloudflare:** beide sites blokkeren gewone scripts. De monitor gebruikt daarom `curl_cffi`, dat zich voordoet als Chrome.
- **Andere zoekpagina's** kun je toevoegen in `SEARCH_URLS`. Alleen pagina 1 wordt gelezen, dus zorg dat ze op "Nieuwste eerst" staan.
