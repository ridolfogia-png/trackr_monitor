#!/usr/bin/env python3
"""
Trackr UK Summer Internships monitor
=====================================
Renders the (JavaScript) Trackr page with a headless browser, reads the
positions TABLE, compares it against the saved state, and emails you when NEW
positions appear. Built to run on GitHub Actions (stateless runners): the state
is committed back to the repository between runs.

NO credentials live in this file. Everything sensitive is read from environment
variables, which on GitHub come from encrypted repository *Secrets*.

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
  TRACKR_DEBUG=1 (optional) discovery mode: print what was parsed and exit,
                            without sending email or changing state.
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

# Default = the categories relevant to an IB / PE candidate. Set CATEGORIES="all"
# (or a custom comma-separated list) to change scope.
DEFAULT_CATEGORIES = ["Promoted", "Bulge Bracket", "Elite Boutique", "Middle Market", "Buy-Side"]
_cat_env = os.environ.get("CATEGORIES", "").strip()
if _cat_env.lower() == "all":
    CATEGORIES = None  # no filtering
elif _cat_env:
    CATEGORIES = [c.strip() for c in _cat_env.split(",") if c.strip()]
else:
    CATEGORIES = DEFAULT_CATEGORIES

# JavaScript run inside the page: locate the positions table by its header
# labels and return one record per row, carrying the current category heading.
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
      // Category heading / separator row -> remember it as the current section.
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
    """Render the page and return (rows, headers)."""
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
        # Give the table time to render, then wait for it explicitly.
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
    """Map raw rows to {key: record}, keyed stably on (company, programme)."""
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
    return any(c.lower() in cat for c in CATEGORIES)


def send_email(subject, body):
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
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
    bits = [f"[{rec['category']}]" if rec.get("category") else ""]
    bits.append(f"{rec.get('company') or '?'} - {rec.get('programme') or '?'}")
    tail = []
    if rec.get("opening"):
        tail.append(f"apertura {rec['opening']}")
    if rec.get("closing"):
        tail.append(f"chiusura {rec['closing']}")
    line = " ".join(b for b in bits if b)
    if tail:
        line += f" ({', '.join(tail)})"
    if rec.get("link"):
        line += f"\n  {rec['link']}"
    return "- " + line


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rows, headers = capture_rows()

    if not rows:
        log("WARN: parsed 0 rows this run (page not rendered?). State unchanged.")
        return 0

    everything = normalize(rows)
    current = {k: v for k, v in everything.items() if in_scope(v)}
    log(f"Parsed {len(everything)} rows; {len(current)} in scope "
        f"(categories={'all' if CATEGORIES is None else ','.join(CATEGORIES)}).")

    if DEBUG:
        log("Headers:", headers)
        from collections import Counter
        cats = Counter(v["category"] for v in everything.values())
        log("Rows per category:", dict(cats))
        log("Sample in-scope records:")
        for v in list(current.values())[:8]:
            log("  ", describe(v).replace("\n", " "))
        return 0

    seen = {}
    if SEEN_FILE.exists():
        try:
            seen = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            seen = {}

    baseline = not seen
    new_keys = [k for k in current if k not in seen]

    merged = {**seen, **current}
    SEEN_FILE.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")

    today = datetime.date.today().isoformat()
    if not HEARTBEAT_FILE.exists() or HEARTBEAT_FILE.read_text(encoding="utf-8").strip() != today:
        HEARTBEAT_FILE.write_text(today, encoding="utf-8")

    if baseline:
        send_email(
            f"[Trackr Monitor] Avviato - {len(current)} posizioni in baseline",
            "Il monitor e' attivo.\n\n"
            "Da ora ricevi una mail solo quando compaiono NUOVE posizioni "
            "summer UK su Trackr nelle categorie monitorate "
            f"({'tutte' if CATEGORIES is None else ', '.join(CATEGORIES)}).\n\n"
            f"Pagina: {TRACKR_URL}\n",
        )
        log(f"Baseline saved ({len(current)} positions).")
        return 0

    if new_keys:
        body = (
            f"{len(new_keys)} nuova/e posizione/i summer UK su Trackr:\n\n"
            + "\n".join(describe(current[k]) for k in new_keys)
            + f"\n\nPagina: {TRACKR_URL}\n"
        )
        send_email(f"🆕 {len(new_keys)} nuova/e posizione/i summer UK su Trackr", body)
    else:
        log("No new positions this run.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
