# Deploy av medlemsräknaren

Det här projektet är en liten Python-webbserver. Rekommenderad live-setup:

- Render/Railway/Fly.io/VPS kör Python-appen
- Cloudflare hanterar domän, DNS, SSL och proxy

## Filer

- `member_stream.py` – själva appen
- `reminders.csv` – lagring av påminnelseanmälningar
- `requirements.txt` – tom, appen använder bara Python-standardbibliotek
- `Procfile` – startkommando för hostar som använder Procfile
- `render.yaml` – färdig Render-konfiguration
- `Dockerfile` – alternativ om du vill köra container

## Snabbast: Render

1. Skapa ett GitHub-repo och ladda upp alla filer i den här mappen.
2. Gå till Render och välj **New +** → **Blueprint** om du vill använda `render.yaml`, eller **Web Service**.
3. Koppla GitHub-repot.
4. Kontrollera att startkommando är:

```bash
python member_stream.py
```

5. Lägg in miljövariablerna nedan.
6. Deploya.
7. Testa:

```text
https://din-render-url.onrender.com/
https://din-render-url.onrender.com/banner
https://din-render-url.onrender.com/member-count
https://din-render-url.onrender.com/debug
```

## Miljövariabler

Grund:

```bash
PORT=8088
CUTOFF_DATE=2026-04-01
LAUNCH_ISO=2026-05-01T00:00:00+02:00
FORCE_LIVE=1
ITARGET_POLL_SECONDS=10
REMINDERS_PATH=/opt/render/project/src/reminders.csv
```

För iTarget-kopplingen, fyll i dessa från webbläsarens Network-request:

```bash
ITARGET_INTERNAL_ENDPOINT=https://app.itarget.se/livewire/message/members-index-new
ITARGET_INTERNAL_METHOD=POST
ITARGET_INTERNAL_HEADERS={"content-type":"application/json","x-livewire":"true","cookie":"..."}
ITARGET_INTERNAL_BODY={...}
ITARGET_SOURCE=members-index-new
ITARGET_INTERNAL_EXPECTED_STATUS=active
```

Obs: cookies/sessioner kan gå ut. Om `/debug` visar session-fel behöver `ITARGET_INTERNAL_HEADERS` uppdateras.

## Cloudflare DNS

När hosten är live:

1. Cloudflare → din domän → **DNS**.
2. Lägg till CNAME:

```text
Type: CNAME
Name: 13000
Target: din-render-url.onrender.com
Proxy status: Proxied
```

Det ger exempelvis:

```text
https://13000.dindomän.se
```

Alternativ:

```text
medlemmar.dindomän.se
rekord.dindomän.se
```

## Bädda in bannern

```html
<iframe
  src="https://13000.dindomän.se/banner"
  style="width:100%;border:0;min-height:420px;"
></iframe>
```

## Bra kontroll före lansering

- `/member-count` visar rätt antal.
- `/debug` saknar `last_error`.
- `/banner` ser bra ut i mobil.
- Cloudflare SSL står på **Full** eller **Full (strict)**.
