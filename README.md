# Trackr Summer UK Monitor

Monitora la pagina **UK Finance – Summer Internships** di The Trackr e ti manda una
**email** (→ notifica push sul telefono) ogni volta che compare una **nuova posizione**.
Gira su **GitHub Actions**, quindi è attivo 24/7 anche a computer spento: non dipende
dall'app Claude.

Pagina monitorata: `https://app.the-trackr.com/uk-finance/summer-internships`

---

## Come funziona (in breve)

Ogni ~15 minuti GitHub avvia il workflow, che: apre la pagina con un browser headless
(così vede le posizioni renderizzate via JavaScript, esattamente come le vedi tu),
confronta l'elenco con lo stato salvato (`state/seen.json`), e se trova qualcosa di
nuovo ti manda una mail. Lo stato viene poi salvato nel repo per il run successivo.

**Aspettative oneste (importante).**
- *Tempo reale* = controllo ogni ~15 min. The Trackr non offre webhook e di suo
  aggiorna entro qualche ora dall'apertura: sarai tra i primi a saperlo, con latenza
  di minuti — non "istantaneo al secondo" (non è possibile con nessuno strumento).
- *Affidabilità* = molto alta, ma lo scheduler gratuito di GitHub è "best-effort" e
  sotto carico può ritardare o, raramente, saltare un run. Per qualcosa di davvero
  mission-critical un Raspberry Pi / micro-VPS con cron è ancora più solido.

---

## Prerequisiti

1. Un account **GitHub** (gratuito).
2. Una **Gmail** con verifica in due passaggi attiva e una **app password** dedicata
   (la password normale non funziona via SMTP). Vedi sotto.

---

## Setup passo-passo

### 1) Crea il repository
- Su GitHub: **New repository** → nome es. `trackr-monitor` → **Public** (consigliato:
  i repo pubblici hanno minuti Actions illimitati; resta visibile solo il codice e i
  nomi pubblici degli internship — la tua email e la password NO, stanno nei Secrets).
- Carica il contenuto di questa cartella `trackr_monitor/` nella **root** del repo
  (così `.github/`, `monitor.py`, `requirements.txt`, `state/` stanno alla radice).
  Puoi trascinare i file dalla UI di GitHub (“Add file → Upload files”) o usare git.

### 2) Crea la app password Gmail
- Vai su **myaccount.google.com** → **Security** → attiva **2-Step Verification** se non
  c'è già.
- Poi **App passwords** (myaccount.google.com/apppasswords) → genera una password per
  un'app “Mail”. Copia i 16 caratteri (li userai come `SMTP_PASS`).

### 3) Imposta i Secrets del repo
Nel repo: **Settings → Secrets and variables → Actions → New repository secret**.
Crea questi:

| Nome        | Valore                                   | Obbligatorio |
|-------------|------------------------------------------|--------------|
| `SMTP_USER` | la tua Gmail, es. `ridolfo.gia@gmail.com`| sì           |
| `SMTP_PASS` | la app password Gmail (16 caratteri)     | sì           |
| `MAIL_TO`   | dove vuoi gli alert (di solito la stessa Gmail) | no (default = SMTP_USER) |
| `SMTP_HOST` | solo se NON usi Gmail                     | no           |
| `SMTP_PORT` | solo se NON usi Gmail                     | no           |

> ⚠️ Non scrivere mai la password dentro i file del repo. Va **solo** nei Secrets:
> sono cifrati e non compaiono nei log, nemmeno in un repo pubblico.

### 4) Abilita e lancia
- Tab **Actions** → se richiesto, abilita i workflow.
- Apri **Trackr Summer UK Monitor** → **Run workflow** (bottone manuale) per il primo run.
- Al **primo run** ricevi una mail “*Avviato – N posizioni in baseline*”: significa che
  funziona. Da lì in poi ti arriva una mail **solo** quando esce una posizione nuova.
- Da ora gira da solo ogni ~15 min.

---

## Verifica / tuning (un passaggio consigliato)

The Trackr è una web-app: lo script **scopre da solo** l'elenco delle posizioni dentro
i dati che la pagina carica. Per essere sicuri al 100% che agganci la lista giusta,
fai un **run di debug** una volta:

- In `monitor.yml`, nello step *Run monitor*, aggiungi temporaneamente `TRACKR_DEBUG: "1"`
  tra le `env:`, lancia il workflow, apri i log dello step e guarda la sezione
  `DEBUG`. Mandami quell'output (gli endpoint trovati + il “sample item”): se serve, ti
  do un parser cucito sui campi reali e — meglio ancora — una **versione che legge
  direttamente l'API JSON** (senza browser): più veloce, più leggera, ancora più
  affidabile. Poi togli `TRACKR_DEBUG`.

In alternativa, se colleghi l'estensione **Claude in Chrome**, posso aprire io la pagina,
vedere la struttura reale e consegnarti il parser già tarato.

---

## File del progetto

```
trackr_monitor/
  monitor.py                  # lo script (scrape + diff + email + baseline + debug)
  requirements.txt            # dipendenze (playwright)
  .github/workflows/monitor.yml  # schedulazione GitHub Actions
  state/seen.json             # stato (aggiornato in automatico dai run)
  README.md                   # questo file
```

## Modifiche rapide
- **Frequenza:** in `monitor.yml`, cambia `cron: "*/15 * * * *"` (min. GitHub = 5 min,
  `*/5`). Più frequente = più reattivo ma più minuti consumati.
- **Categorie:** di default ti avviso solo sulle categorie in target IB/PE
  (`Promoted, Bulge Bracket, Elite Boutique, Middle Market, Buy-Side`). Per ricevere
  TUTTO (anche consulting, asset management, insurance, ecc.) aggiungi un secret
  `CATEGORIES` con valore `all`, oppure una tua lista separata da virgole.
- **Cosa monitorare:** per seguire anche gli off-cycle, duplica il workflow puntando
  `TRACKR_URL` a `https://app.the-trackr.com/uk-finance/off-cycle-internships`.
