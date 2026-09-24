# Woning-monitor Amsterdam

Checkt elke 15 minuten (vanaf een pc thuis) Pararius en Huurwoningen.nl, leest de beschrijving van elke nieuwe woning en mailt alleen de woningen die passen.

## Wat hij filtert

- Max €3000 p/m, 4+ kamers
- Binnen de ring (Centrum, West, Zuid, Oost), op basis van postcode
- Oost alleen als het een goede deal is (max €625 per kamer)
- Afgewezen als de beschrijving zegt: geen studenten, geen garantsteller, geen woningdelers, of als het een woningruil is
- Waarschuwing (wel gemaild) bij: inkomenseis, max 2 personen/woningdelers, geen friends-contract, inkomen uit werk vereist
- Dezelfde woning op beide sites krijg je maar één keer (herkend aan postcode + prijs)

Alles is aan te passen in `config.py`. Onderaan elke mail staat wat er is weggefilterd en waarom, zodat je kan zien of het filter goed werkt.

## Installatie op je eigen pc (Windows)

Pararius en Huurwoningen zitten achter Cloudflare, dat de servers van GitHub
blokkeert (403). Vanaf een gewone thuisverbinding werkt het wel, daarom draait de
monitor via Windows Taakplanner op een pc thuis. **Die pc moet aan staan**; na
slaapstand haalt hij een gemiste check meteen in.

**1. Gmail app-wachtwoord maken**
- Zet tweestapsverificatie aan op je Google-account (vereist)
- Ga naar https://myaccount.google.com/apppasswords en maak een wachtwoord aan

**2. Python-omgeving** (in deze map, eenmalig)
```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

**3. `.env` invullen** (staat naast `monitor.py`, gaat niet mee naar GitHub)
```
GMAIL_USER=jij@gmail.com
GMAIL_APP_PASSWORD=abcdefghijklmnop
EMAIL_TO=jij@gmail.com,huisgenoot@gmail.com
```

**4. Taak in Taakplanner** (PowerShell, in deze map)
```
$d = (Get-Location).Path
$a = New-ScheduledTaskAction -Execute "$d\.venv\Scripts\pythonw.exe" -Argument monitor.py -WorkingDirectory $d
$t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15)
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName Woning-monitor -Action $a -Trigger $t -Settings $s
```
De output komt in `monitor.log`. Zolang `.env` leeg is, slaat de taak elke run over.
De eerste echte run mailt alles wat nu online staat en aan je criteria voldoet
(max 25 woningen per run), daarna alleen nog nieuwe woningen.

**Stoppen:** `Unregister-ScheduledTask -TaskName Woning-monitor` of via Taakplanner.

## Handig

- **Eén woning testen:** `python monitor.py --test <url>` laat zien wat de tool eruit haalt en of hij 'm zou doorsturen.
- **Filter aangepast?** Draai `python test_filters.py`: dat checkt de filters op zinnen uit echte advertenties.
- **Problemen?** Als een site blokkeert, of een zoekpagina 3 runs op rij faalt of 0 woningen geeft (bijv. omdat de URL veranderd is), krijg je max 1x per dag een waarschuwingsmail.
- **Cloudflare:** beide sites blokkeren gewone scripts. De monitor gebruikt daarom `curl_cffi`, dat zich voordoet als Chrome.
- **Andere zoekpagina's** kun je toevoegen in `SEARCH_URLS`. Alleen pagina 1 wordt gelezen, dus zorg dat ze op "Nieuwste eerst" staan.
