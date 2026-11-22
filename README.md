# merit-aktiva-mcp

An MCP server that gives Claude your **Merit Aktiva** bookkeeping (Estonia): every endpoint of
Merit's API as a tool. Ask Claude what customers owe, how the month went, which bills are open;
with writing switched on, let it enter invoices, record payments, import bank statements and keep
master data in order.

> **Unofficial.** Not made, endorsed or supported by Merit Tarkvara AS. "Merit Aktiva" is their
> product and trademark.

*Eesti keeles: [allpool](#eesti-keeles).*

## What you need

- A Merit Aktiva company (Estonian version, aktiva.merit.ee) with a **PRO or Premium** licence:
  Merit's API works only with those.
- An API ID and key: in Merit Aktiva, **Seadistused → API seadistus → Loo võti**.
- Claude: Claude Desktop or Claude Code on your computer, or claude.ai with your own server.

## Two ways to run it

| | Local (your computer) | Server (claude.ai, teams, phone) |
|---|---|---|
| Works in | Claude Desktop, Claude Code | claude.ai web, desktop and mobile apps |
| Setup | Install the extension, or one config entry | Docker, an HTTPS address, a Microsoft or Google sign-in app |
| Who uses it | You | Everyone on your allow-list |
| Files (PDFs, statements) | Paths on your computer | Upload links |

### Local: Claude Desktop extension (easiest)

1. Download `merit-aktiva-mcp.mcpb` from the [latest release](https://github.com/lauri911/merit-aktiva-mcp/releases/latest).
2. Open it with Claude Desktop (or drag it into **Settings → Extensions**).
3. Enter your API ID and key. Leave **Allow writing** off until you want Claude to change things.

### Local: by hand (Claude Desktop or Claude Code)

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then add this to Claude
Desktop's `claude_desktop_config.json` (**Settings → Developer → Edit Config**):

```json
{
  "mcpServers": {
    "merit": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/lauri911/merit-aktiva-mcp", "merit-aktiva-mcp"],
      "env": {
        "MERIT_API_ID": "your API ID",
        "MERIT_API_KEY": "your API key",
        "MERIT_ALLOW_WRITE": "false"
      }
    }
  }
}
```

Claude Code:

```bash
claude mcp add merit --env MERIT_API_ID=... --env MERIT_API_KEY=... -- uvx --from git+https://github.com/lauri911/merit-aktiva-mcp merit-aktiva-mcp
```

**Several companies** (an accounting firm, for example): add one entry per company, each with its
own name (`merit-acme`, `merit-beta`) and its own API key. One server talks to one company.
Set `MERIT_COMPANY_NAME` on each, so Claude knows whose books each server holds and every write
preview says which company it goes into.

### Server: for claude.ai and teams

Run it with Docker behind HTTPS, with sign-in through **Microsoft Entra ID** (Microsoft 365) or
**Google**, limited to the email addresses you list. Step by step: [docs/server-setup.md](docs/server-setup.md).

## Settings

| Variable | Meaning |
|---|---|
| `MERIT_API_ID`, `MERIT_API_KEY` | Required. From Seadistused → API seadistus |
| `MERIT_ALLOW_WRITE` | `true` to also offer the write tools. Default: read only |
| `MERIT_COMPANY_NAME` | Optional. The company's name, e.g. `Acme OÜ`: in the server's name, its instructions and every write preview and result. Set it when you connect more than one company |
| `MERIT_EXTRA_INSTRUCTIONS` | Anything Claude should know about how your company does its books (or `MERIT_EXTRA_INSTRUCTIONS_FILE`) |
| `MERIT_BASE_URL` | Default `https://aktiva.merit.ee/api`. Merit's Polish and Finnish versions have other addresses and other VAT rules: untested |

Server-only settings are in [docs/server-setup.md](docs/server-setup.md). Any setting can also be
given as a file: `MERIT_API_KEY_FILE=/run/secrets/merit_api_key`.

## What Claude can do

**Read** (always): sales and purchase invoices (with the printed PDF or the vendor's file), invoices
waiting for approval, sales offers, recurring invoices, payments, bank entries and statement imports,
customer debts and payment report, profit and loss, balance sheet, sales and purchase reports,
general ledger entries, stock movements and stock report, fixed assets, price lists and discounts,
customers, vendors, and all master data (accounts, banks, VAT rates, departments, dimensions, items,
groups, units, payment types, stocks, financial years).

**Write** (with `MERIT_ALLOW_WRITE=true`): sales invoices (create, send by email or e-invoice,
replace or delete if unpaid and unsent, credit invoice), purchase invoices (create, replace, delete),
sales offers, recurring invoices, payments (invoice and offer payments, other bank income and
expenses, prepayments, offsetting, deleting, CAMT.053 statement import), general ledger entries,
stock movements, fixed assets, e-invoice XML import, customer and vendor cards, items, groups,
units, dimensions, OSS VAT rates, sales prices and discounts.

## Safety

Writes are real bookkeeping entries, so read [docs/safety.md](docs/safety.md). In short: writing is
off until you switch it on; Claude Desktop asks before each write unless you choose "always allow";
every write tool can do a **dry run** that shows exactly what would be written; and the tools refuse
the mistakes Merit itself would let through (a vendor's bill entered twice, paying more than is
owed, deleting a paid or sent invoice, VAT from a vendor not registered for VAT). There is no
separate approval step: deciding when to check with you is Claude's job.

## Good to know

Merit's API has quirks that the tools handle; they are listed in [docs/merit-notes.md](docs/merit-notes.md).
Merit has no sandbox: to try writing safely, create a separate test company in Merit and run
[scripts/live-test.py](scripts/live-test.py) against it.

## Development

```bash
uv run --group dev pytest -q        # tests, with a fake Merit
uv run merit-aktiva-mcp --help
```

Licence: [MIT](LICENSE).

---

## Eesti keeles

MCP-server, mis annab Claude'ile ligipääsu sinu **Merit Aktiva** raamatupidamisele: kõik Meriti API
otspunktid tööriistadena. Küsi Claude'ilt, kes on võlgu, kuidas kuu läks, millised arved on
tasumata; kui kirjutamine on lubatud, lase tal sisestada arveid, märkida makseid, importida
pangaväljavõtteid ja hoida põhiandmeid korras.

> **Mitteametlik.** Ei ole Merit Tarkvara AS-i tehtud, heaks kiidetud ega toetatud. "Merit Aktiva"
> on nende toode ja kaubamärk.

### Mida on vaja

- Merit Aktiva ettevõte (Eesti versioon, aktiva.merit.ee) **PRO või Premium** litsentsiga: Meriti
  API töötab ainult nendega.
- API ID ja võti: Merit Aktivas **Seadistused → API seadistus → Loo võti**.
- Claude: Claude Desktop või Claude Code sinu arvutis või claude.ai koos oma serveriga.

### Kaks võimalust

| | Kohalik (sinu arvuti) | Server (claude.ai, meeskond, telefon) |
|---|---|---|
| Töötab | Claude Desktopis, Claude Code'is | claude.ai veebis, töölaua- ja mobiilirakendustes |
| Seadistamine | Paigalda laiendus või lisa üks seadistuse kirje | Docker, HTTPS-aadress, Microsofti või Google'i sisselogimise rakendus |
| Kes kasutab | Sina | Kõik lubatud e-posti aadressid |
| Failid (PDF-id, väljavõtted) | Failiteed sinu arvutis | Üleslaadimise lingid |

#### Kohalik: Claude Desktopi laiendus (kõige lihtsam)

1. Laadi [viimasest väljalaskest](https://github.com/lauri911/merit-aktiva-mcp/releases/latest) alla `merit-aktiva-mcp.mcpb`.
2. Ava see Claude Desktopiga (või lohista **Settings → Extensions** alla).
3. Sisesta API ID ja võti. Jäta **Allow writing** välja, kuni tahad, et Claude midagi muudaks.

#### Kohalik: käsitsi (Claude Desktop või Claude Code)

Paigalda [uv](https://docs.astral.sh/uv/getting-started/installation/) ja lisa Claude Desktopi
`claude_desktop_config.json` faili (**Settings → Developer → Edit Config**) ülal näidatud
`mcpServers` kirje oma API ID ja võtmega. Claude Code'is käivita ülal näidatud `claude mcp add`
käsk.

**Mitu ettevõtet** (näiteks raamatupidamisbüroo): lisa iga ettevõtte jaoks oma kirje, igaühel oma
nimi (`merit-acme`, `merit-beta`) ja oma API võti. Üks server töötab ühe ettevõttega.
Pane igaühele `MERIT_COMPANY_NAME`, siis teab Claude, kelle raamatud igas serveris on, ja iga
kirjutamise eelvaade näitab, millisesse ettevõttesse see läheb.

#### Server: claude.ai ja meeskonna jaoks

Käivita see Dockeriga HTTPS-i taga, sisselogimisega **Microsoft Entra ID** (Microsoft 365) või
**Google**'i kaudu, ainult sinu loetletud e-posti aadressidele. Juhend (inglise keeles):
[docs/server-setup.md](docs/server-setup.md).

### Seaded

| Muutuja | Tähendus |
|---|---|
| `MERIT_API_ID`, `MERIT_API_KEY` | Kohustuslik. Seadistused → API seadistus |
| `MERIT_ALLOW_WRITE` | `true`, et pakkuda ka kirjutavaid tööriistu. Vaikimisi ainult lugemine |
| `MERIT_COMPANY_NAME` | Valikuline. Ettevõtte nimi, nt `Acme OÜ`: serveri nimes, juhistes ning iga kirjutamise eelvaates ja tulemuses. Pane see, kui ühendad mitu ettevõtet |
| `MERIT_EXTRA_INSTRUCTIONS` | Mida Claude peaks teadma sinu ettevõtte raamatupidamisest (või `MERIT_EXTRA_INSTRUCTIONS_FILE`) |
| `MERIT_BASE_URL` | Vaikimisi `https://aktiva.merit.ee/api`. Meriti Poola ja Soome versioonidel on teised aadressid ja teised käibemaksureeglid: testimata |

### Mida Claude teha saab

**Lugeda** (alati): müügi- ja ostuarved (koos prinditud PDF-i või tarnija failiga), kinnitamata
ostuarved, pakkumised, perioodilised arved, maksed, panga sissetulekud ja väljaminekud ning
väljavõtete impordid, klientide võlad ja laekumiste aruanne, kasumiaruanne, bilanss, müügi- ja
ostuaruanded, pearaamatu kanded, laoliikumised ja laoseis, põhivara, hinnakirjad ja allahindlused,
kliendid, tarnijad ning kõik põhiandmed.

**Kirjutada** (kui `MERIT_ALLOW_WRITE=true`): müügiarved (koostamine, saatmine e-postiga või
e-arvena, asendamine või kustutamine, kui tasumata ja saatmata, kreeditarve), ostuarved, pakkumised,
perioodilised arved, maksed (arvete ja pakkumiste tasumine, muud panga sissetulekud ja väljaminekud,
ettemaksed, tasaarveldused, kustutamine, CAMT.053 väljavõtte import), pearaamatu kanded,
laoliikumised, põhivara, e-arve XML import, kliendi- ja tarnijakaardid, artiklid, grupid, ühikud,
dimensioonid, OSS käibemaksumäärad, müügihinnad ja allahindlused.

### Ohutus

Kirjutamine teeb päris raamatupidamiskandeid, seega loe [docs/safety.md](docs/safety.md). Lühidalt:
kirjutamine on välja lülitatud, kuni sa selle sisse lülitad; Claude Desktop küsib enne iga
kirjutamist, kui sa ei vali "always allow"; iga kirjutav tööriist oskab teha **proovikäivituse**
(dry run), mis näitab täpselt, mis kirja läheks; ja tööriistad keelduvad vigadest, mida Merit ise
läbi laseks (sama tarnija arve kaks korda, võlast suurem makse, tasutud või saadetud arve
kustutamine, käibemaks tarnijalt, kes pole käibemaksukohustuslane). Eraldi kinnitamise sammu ei ole:
millal sinult üle küsida, otsustab Claude.

### Hea teada

Meriti API eripärad, millega tööriistad arvestavad, on kirjas failis [docs/merit-notes.md](docs/merit-notes.md).
Meritil pole testkeskkonda: kirjutamise ohutuks proovimiseks loo Meritis eraldi testettevõte ja
käivita selle vastu [scripts/live-test.py](scripts/live-test.py).

Litsents: [MIT](LICENSE).
