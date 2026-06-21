#!/usr/bin/env python3
"""
Trackr UK Summer Internships monitor
=====================================
Renders the (JavaScript) Trackr page with a headless browser, reads the
positions TABLE, and emails you when a watched programme OPENS -- i.e. when its
Opening/Closing date cells go from empty to filled (the signal Trackr uses when
applications go live, e.g. Blackstone: "15 Jun 26 -> 30 Oct 26").

Built for GitHub Actions (stateless runners): the state is committed back to the
repository between runs. NO credentials live in this file -- everything sensitive
comes from environment variables (encrypted repository *Secrets* on GitHub).

Environment variables
----------------------
  SMTP_USER   (required)  e.g. your Gmail address (also default "From")
  SMTP_PASS   (required)  app password (NOT your normal password) - see README
  MAIL_TO     (optional)  where alerts go (defaults to SMTP_USER)
  SMTP_HOST   (optional)  default "smtp.gmail.com"
  SMTP_PORT   (optional)  default "587"
  TRACKR_URL  (optional)  page to watch (default = UK finance summer internships)
  CATEGORIES  (optional)  comma-separated Trackr categories to watch.
                          Default focuses on IB/PE; use "all" to watch every row.
  TRACKR_DEBUG=1 (optional) discovery mode: print what was parsed and exit.
"""

import os
import sys
import ssl
import json
import smtplib
import datetime
import pathlib
from email.message import EmailMessage

from playwright.sync_api import sync_playwright

TRACKR_URL = os.environ.get(
    "TRACKR_URL", "https://app.the-trackr.com/uk-finance/summer-internships"
)
STATE_DIR = pathlib.Path(os.environ.get("STATE_DIR", "state"))
SEEN_FILE = STATE_DIR / "seen.json"
HEARTBEAT_FILE = STATE_DIR / "last_check_date.txt"
DEBUG = os.environ.get("TRACKR_DEBUG") == "1"

# Categories relevant to an IB / PE candidate. Set CATEGORIES="all" to watch all.
DEFAULT_CATEGORIES = ["Promoted", "Bulge Bracket", "Elite Boutique", "Middle Market", "Buy-Side"]
_cat_env = os.environ.get("CATEGORIES", "").strip()
if _cat_env.lower() == "all":
    CATEGORIES = None
elif _cat_env:
    CATEGORIES = [c.strip() for c in _cat_env.split(",") if c.strip()]
else:
    CATEGORIES = DEFAULT_CATEGORIES

# Safety net: a role whose title/company contains any of these is watched even if
# Trackr files it under a non-target category (e.g. Consulting / Miscellaneous).
KEYWORDS = [
    "m&a", "corporate finance", "restructuring", "private equity", "leveraged finance",
    "investment banking", "capital markets", "financial advisory", "advisory",
    "financial sponsors", "growth equity",
]

# JS run inside the page: locate the positions table by header labels and return
# one record per row, carrying the current category heading.
EXTRACT_JS = r"""
() => {
  const tables = Array.from(document.querySelectorAll('table'));
  let best = null;
  for (const t of tables) {
    const txt = (t.innerText || '').toLowerCase();
    if (txt.includes('company') && txt.includes('programme')) { best = t; break; }
  }
  if (!best) best = document.querySelector('table');
  if (!best) return { headers: [], rows: [] };

  const rowEls = Array.from(best.querySelectorAll('tr'));
  let headerIdx = rowEls.findIndex(r => {
    const t = (r.innerText || '').toLowerCase();
    return t.includes('company') && t.includes('programme');
  });
  const headerCells = headerIdx >= 0
    ? Array.from(rowEls[headerIdx].querySelectorAll('th,td')).map(c => c.innerText.trim())
    : [];
  const col = (name) => headerCells.findIndex(h => h.toLowerCase().includes(name));
  const ci = {
    company: col('company'),
    programme: col('programme'),
    opening: col('opening'),
    closing: col('closing'),
  };

  const rows = [];
  let currentCat = '';
  const start = headerIdx >= 0 ? headerIdx + 1 : 0;
  for (let i = start; i < rowEls.length; i++) {
    const cellEls = Array.from(rowEls[i].querySelectorAll('td'));
    const cells = cellEls.map(c => c.innerText.trim());
    const nonEmpty = cells.filter(Boolean);
    if (cells.length < 3 || nonEmpty.length < 2) {
      const t = rowEls[i].innerText.trim();
      if (t && t.length < 40) currentCat = t;
      continue;
    }
    const pick = (idx, fb) => (idx >= 0 && cells[idx] ? cells[idx] : (cells[fb] || ''));
    const company = pick(ci.company, 1);
    const programme = pick(ci.programme, 2);
    if (!company && !programme) continue;
    const a = rowEls[i].querySelector('a[href]');
    rows.push({
      category: currentCat,
      company: company,
      programme: programme,
      opening: ci.opening >= 0 ? (cells[ci.opening] || '') : '',
      closing: ci.closing >= 0 ? (cells[ci.closing] || '') : '',
      link: a ? a.href : '',
    });
  }
  return { headers: headerCells, rows: rows };
}
"""


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def capture_rows():
    data = {"headers": [], "rows": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 TrackrMonitor/1.0"
            )
        )
        page = ctx.new_page()
        try:
            page.goto(TRACKR_URL, wait_until="networkidle", timeout=60000)
        except Exception as e:
            log("WARN goto:", e)
            try:
                page.goto(TRACKR_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e2:
                log("ERROR goto retry:", e2)
        try:
            page.wait_for_selector("table", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        try:
            data = page.evaluate(EXTRACT_JS)
        except Exception as e:
            log("ERROR evaluate:", e)
        browser.close()
    return data.get("rows", []), data.get("headers", [])


def normalize(rows):
    out = {}
    for r in rows:
        company = str(r.get("company", "")).strip()
        programme = str(r.get("programme", "")).strip()
        if not company and not programme:
            continue
        key = json.dumps([company.lower(), programme.lower()], ensure_ascii=False)
        out[key] = {
            "category": str(r.get("category", "")).strip(),
            "company": company,
            "programme": programme,
            "opening": str(r.get("opening", "")).strip(),
            "closing": str(r.get("closing", "")).strip(),
            "link": r.get("link", "") or "",
        }
    return out


def in_scope(rec):
    if CATEGORIES is None:
        return True
    cat = (rec.get("category") or "").lower()
    if any(c.lower() in cat for c in CATEGORIES):
        return True
    text = ((rec.get("company") or "") + " " + (rec.get("programme") or "")).lower()
    return any(kw in text for kw in KEYWORDS)


def is_open(rec):
    """A programme is 'open' once Trackr fills its Opening or Closing date."""
    return bool((rec.get("opening") or "").strip() or (rec.get("closing") or "").strip())


def send_email(subject, body):
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = os.environ["SMTP_USER"]
    pw = os.environ["SMTP_PASS"]
    to = os.environ.get("MAIL_TO") or user
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, pw)
        s.send_message(msg)
    log(f"Email sent to {to}: {subject}")


def describe(rec):
    head = f"[{rec['category']}]" if rec.get("category") else ""
    line = f"{head} {rec.get('company') or '?'} - {rec.get('programme') or '?'}".strip()
    dates = []
    if rec.get("opening"):
        dates.append(f"apre {rec['opening']}")
    if rec.get("closing"):
        dates.append(f"chiude {rec['closing']}")
    if dates:
        line += " (" + ", ".join(dates) + ")"
    if rec.get("link"):
        line += f"\n  {rec['link']}"
    return "- " + line


def was_open(prev):
    return bool(prev and (str(prev.get("opening") or "").strip() or str(prev.get("closing") or "").strip()))


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rows, headers = capture_rows()

    if not rows:
        log("WARN: parsed 0 rows this run (page not rendered?). State unchanged.")
        return 0

    everything = normalize(rows)
    current = {k: v for k, v in everything.items() if in_scope(v)}
    open_now = sum(1 for v in current.values() if is_open(v))
    log(f"Parsed {len(everything)} rows; {len(current)} watched; {open_now} open now "
        f"(categories={'all' if CATEGORIES is None else ','.join(CATEGORIES)} + keywords).")

    if DEBUG:
        log("Headers:", headers)
        from collections import Counter
        cats = Counter(v["category"] for v in everything.values())
        log("Rows per category:", dict(cats))
        log("Currently OPEN (watched):")
        for v in current.values():
            if is_open(v):
                log("  ", describe(v).replace("\n", " "))
        return 0

    seen = {}
    if SEEN_FILE.exists():
        try:
            seen = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            seen = {}

    baseline = not seen

    # Daily heartbeat keeps the repo active (avoids 60-day auto-disable).
    today = datetime.date.today().isoformat()
    if not HEARTBEAT_FILE.exists() or HEARTBEAT_FILE.read_text(encoding="utf-8").strip() != today:
        HEARTBEAT_FILE.write_text(today, encoding="utf-8")

    if baseline:
        SEEN_FILE.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        send_email(
            f"[Trackr Monitor] Avviato - {len(current)} in osservazione, {open_now} gia aperte",
            "Il monitor e' attivo.\n\n"
            f"Tengo d'occhio {len(current)} programmi summer UK in target "
            f"(categorie IB/PE + ruoli M&A/Restructuring/PE in qualsiasi categoria).\n"
            f"Di questi, {open_now} risultano gia aperti adesso.\n\n"
            "Da ora ti scrivo SOLO quando uno di questi APRE le candidature "
            "(quando su Trackr compaiono le date di apertura/chiusura).\n\n"
            f"Pagina: {TRACKR_URL}\n",
        )
        log(f"Baseline saved: {len(current)} watched, {open_now} already open.")
        return 0

    # Alert on programmes that became OPEN since last time (date cells now filled).
    newly_open = [
        k for k, v in current.items()
        if is_open(v) and not was_open(seen.get(k))
    ]

    merged = {**seen, **current}
    SEEN_FILE.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")

    if newly_open:
        body = (
            "Queste posizioni summer UK risultano ORA aperte su Trackr "
            "(prima non avevano date):\n\n"
            + "\n".join(describe(current[k]) for k in newly_open)
            + f"\n\nPagina: {TRACKR_URL}\n"
        )
        send_email(
            f"🆕 {len(newly_open)} posizione/i APERTA/E su Trackr (summer UK)",
            body,
        )
    else:
        log("No newly opened positions this run.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
