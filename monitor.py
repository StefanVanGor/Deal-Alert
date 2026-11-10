#!/usr/bin/env python3
"""Willhaben- und Vinted-Monitor: meldet neue Anzeigen per ntfy-Push.

Nur Standardbibliothek, es muss nichts installiert werden.

  python3 monitor.py             normaler Lauf (braucht Umgebungsvariable NTFY_TOPIC)
  python3 monitor.py --dry-run   zeigt Treffer nur an, sendet und speichert nichts
  python3 monitor.py --test      sendet eine Test-Push-Nachricht
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
STATE_FILE = ROOT / "seen.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
MAX_SEEN = 3000          # gemerkte Anzeigen pro Suche
MAX_LISTINGS = 300       # Treffer, die die Web-App anzeigt
MAX_SINGLE_PUSHES = 5    # mehr neue Treffer pro Lauf -> Sammelnachricht
FAIL_ALERT_AFTER = 6     # so viele Läufe in Folge ohne Daten -> Warn-Push


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- HTTP

def http_get(url, opener=None, headers=None) -> str:
    h = {"User-Agent": USER_AGENT, "Accept-Language": "de-AT,de;q=0.9,en;q=0.5"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    opener = opener or urllib.request.build_opener()
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        snippet = e.read()[:300].decode("utf-8", errors="replace").replace("\n", " ")
        raise RuntimeError(f"HTTP {e.code} bei {url} -> {snippet}") from None


def to_float(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------ Willhaben

def attr_map(ad: dict) -> dict:
    out = {}
    for a in (ad.get("attributes") or {}).get("attribute") or []:
        vals = a.get("values") or []
        out[a.get("name")] = vals[0] if vals else ""
    return out


def fetch_willhaben(keyword: str, search: dict, cfg: dict) -> list[dict]:
    params = {"keyword": keyword, "sort": "1", "rows": "60", "paylivery": "true"}  # nur PayLivery/Käuferschutz
    if search.get("max_price") is not None:
        params["PRICE_TO"] = str(int(search["max_price"]))
    if search.get("min_price") is not None:
        params["PRICE_FROM"] = str(int(search["min_price"]))
    category_path = (search.get("category_path") or "").strip().strip("/")
    base_path = "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz"
    if category_path:
        base_path += "/" + category_path
    url = base_path + "?" + urllib.parse.urlencode(params)

    html = http_get(url)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("Willhaben: Suchdaten nicht gefunden (Seite geändert oder Zugriff blockiert)")
    try:
        sr = json.loads(m.group(1))["props"]["pageProps"]["searchResult"]
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError(f"Willhaben: unerwartete Datenstruktur ({e!r})")
    ads = (sr.get("advertSummaryList") or {}).get("advertSummary") or []

    items = []
    for ad in ads:
        a = attr_map(ad)
        ad_id = str(ad.get("id") or a.get("ADID") or "")
        if not ad_id:
            continue
        seo = (a.get("SEO_URL") or "").strip()
        if seo.startswith("http"):
            link = seo
        elif seo:
            seo = seo.lstrip("/")
            if not seo.startswith("iad/"):
                seo = "iad/" + seo
            link = "https://www.willhaben.at/" + seo
        else:
            link = f"https://www.willhaben.at/iad/object?adId={ad_id}"
        items.append({
            "platform": "willhaben",
            "id": ad_id,
            "title": (ad.get("description") or a.get("HEADING") or "").strip(),
            "price": to_float(a.get("PRICE")),
            "extra": a.get("LOCATION", ""),
            "url": link,
        })
    return items


# -------------------------------------------------------------------------- Vinted

_openers: dict = {}


def vinted_opener(domain: str):
    """Initialisiert eine anonyme Vinted-Websession und hält deren Cookies."""
    if domain not in _openers:
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        # Vinted stellt die benötigten anonymen Session-Cookies bereits auf HEAD aus.
        req = urllib.request.Request(f"https://{domain}/", headers={"User-Agent": USER_AGENT}, method="HEAD")
        try:
            with op.open(req, timeout=30):
                pass
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Vinted Session-Start fehlgeschlagen: HTTP {e.code}") from None
        cookies = {c.name: c.value for c in jar}
        if not cookies.get("access_token_web") or not cookies.get("anon_id"):
            # Manche Regionen liefern Cookies erst bei einem normalen Seitenaufruf.
            http_get(f"https://{domain}/", opener=op)
            cookies = {c.name: c.value for c in jar}
        if not cookies.get("access_token_web") or not cookies.get("anon_id"):
            raise RuntimeError("Vinted: Session-Cookies access_token_web/anon_id fehlen")
        _openers[domain] = (op, cookies)
    return _openers[domain]


def fetch_vinted(keyword: str, search: dict, cfg: dict) -> list[dict]:
    domain = cfg.get("vinted_domain", "www.vinted.at")
    op, cookies = vinted_opener(domain)
    api_domain = domain.removeprefix("www.")
    params = {
        "search_text": keyword,
        "order": "newest_first",
        "per_page": "24",
        "page": "1",
    }
    if search.get("max_price") is not None:
        params["price_to"] = str(search["max_price"])
    if search.get("min_price") is not None:
        params["price_from"] = str(search["min_price"])
    url = f"https://api.{api_domain}/svc-catalogue/items?" + urllib.parse.urlencode(params)

    body = http_get(
        url,
        opener=op,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Authorization": "Bearer " + cookies["access_token_web"],
            "x-anon-id": cookies["anon_id"],
            "Locale": "de-AT",
            "Referer": f"https://{domain}/catalog",
            "Origin": f"https://{domain}",
        },
    )
    try:
        data = json.loads(body)
    except ValueError:
        raise RuntimeError("Vinted: keine JSON-Antwort (neuer Katalog-Endpunkt blockiert?)")
    if not isinstance(data, dict):
        raise RuntimeError("Vinted: unerwartete Antwort")
    raw_items = data.get("items")
    if raw_items is None:
        # Die neue Schnittstelle kann Treffer auch unter catalog_items liefern.
        raw_items = data.get("catalog_items")
    if not isinstance(raw_items, list):
        detail = data.get("message") or data.get("error") or data.get("code") or "items-Feld fehlt"
        raise RuntimeError(f"Vinted: unerwartete API-Antwort ({detail})")

    items = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        price = it.get("price")
        if isinstance(price, dict):
            price = price.get("amount")
        box = it.get("item_box") or {}
        title = (it.get("title") or "").strip()
        first_line = (box.get("first_line") or "").strip()
        second_line = (box.get("second_line") or "").strip()
        extra_parts = []
        if first_line and first_line.lower() != title.lower():
            extra_parts.append(first_line)
        if second_line:
            extra_parts.append(second_line)
        item_id = it.get("id")
        if not item_id:
            continue
        link = it.get("url") or f"https://{domain}/items/{item_id}"
        if link.startswith("/"):
            link = f"https://{domain}{link}"
        items.append({
            "platform": "vinted",
            "id": str(item_id),
            "title": title,
            "price": to_float(price),
            "extra": " · ".join(extra_parts),
            "url": link,
        })
    return items

FETCHERS = {"willhaben": fetch_willhaben, "vinted": fetch_vinted}


# -------------------------------------------------------------------------- Filter

def matches(item: dict, s: dict) -> bool:
    title = item["title"].lower()
    p = item["price"]
    if p is not None:
        if s.get("max_price") is not None and p > s["max_price"]:
            return False
        if s.get("min_price") is not None and p < s["min_price"]:
            return False
    if any(w.lower() in title for w in s.get("exclude") or []):
        return False
    must = s.get("title_contains_any") or []
    if must and not any(w.lower() in title for w in must):
        return False
    return True


# ------------------------------------------------------------------------- Push

def push(server, topic, title, message, click=None, tags=None, priority=4) -> None:
    payload = {"topic": topic, "title": title, "message": message, "priority": priority}
    if click:
        payload["click"] = click
    if tags:
        payload["tags"] = tags
    req = urllib.request.Request(
        server,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "deal-alert"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def fmt_price(p) -> str:
    if p is None:
        return "Preis n/a"
    return f"€ {p:.0f}" if float(p).is_integer() else f"€ {p:.2f}".replace(".", ",")


# ------------------------------------------------------------------------ Status

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except ValueError:
            log("Warnung: seen.json unlesbar, starte mit leerem Stand")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    args = set(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in args
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")

    if "--test" in args:
        if not topic:
            log("NTFY_TOPIC fehlt.")
            return 1
        push(server, topic, "Deal-Alert Test", "Push-Benachrichtigungen funktionieren.", tags=["white_check_mark"])
        log("Test-Push gesendet.")
        return 0

    if not topic and not dry:
        log("NTFY_TOPIC fehlt (als GitHub-Secret bzw. Umgebungsvariable setzen).")
        return 1

    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    state = load_state()
    seen_all = state.setdefault("seen", {})
    initialized = set(state.get("initialized", []))
    fails = state.setdefault("fails", {})

    pending: dict = {}  # (platform, id) -> {"item", "name", "skeys"}
    current_listings: dict = {}  # aktuell passende Anzeigen; alte, ungeprüfte Einträge verschwinden

    try:
        for platform in cfg.get("platforms", ["willhaben", "vinted"]):
            fetcher = FETCHERS.get(platform)
            if not fetcher:
                log(f"Unbekannte Plattform: {platform}")
                continue
            ok = errs = 0

            for s in cfg["searches"]:
                skey = f"{platform}:{s['name']}"
                seen = seen_all.setdefault(skey, [])
                seen_set = set(seen)

                for kw in s["keywords"]:
                    ikey = f"{skey}:{kw}"
                    try:
                        items = fetcher(kw, s, cfg)
                        ok += 1
                    except Exception as e:  # noqa: BLE001 - Fehler pro Suche isolieren
                        errs += 1
                        log(f"[{ikey}] Fehler: {e}")
                        time.sleep(random.uniform(1, 2))
                        continue

                    first_run = ikey not in initialized
                    new = 0
                    for it in items:
                        if matches(it, s):
                            current_listings.setdefault((platform, it["id"]), {
                                "platform": platform, "id": it["id"], "title": it["title"],
                                "price": it["price"], "extra": it["extra"], "url": it["url"],
                                "search": s["name"],
                                "paylivery": platform == "willhaben",
                                "buyer_protection": platform == "vinted"
                            })
                        if it["id"] in seen_set:
                            continue
                        if first_run or not matches(it, s):
                            seen_set.add(it["id"])
                            seen.append(it["id"])
                            continue
                        new += 1
                        entry = pending.setdefault(
                            (platform, it["id"]), {"item": it, "name": s["name"], "skeys": set()}
                        )
                        entry["skeys"].add(skey)
                    initialized.add(ikey)
                    log(f"[{ikey}] {len(items)} Anzeigen, {new} neu"
                        + (" (Erstlauf: Bestand gemerkt, keine Pushes)" if first_run else ""))
                    time.sleep(random.uniform(1, 3))

                    # Treffer, die schon in einer früheren Suche derselben Plattform
                    # gemeldet wurden, werden über 'pending' nur einmal verschickt.

            if ok == 0 and errs > 0:
                fails[platform] = fails.get(platform, 0) + 1
                if fails[platform] == FAIL_ALERT_AFTER and topic and not dry:
                    try:
                        push(server, topic, f"{platform.title()}: keine Daten",
                             f"{platform.title()} liefert seit {FAIL_ALERT_AFTER} Läufen keine Ergebnisse "
                             "(blockiert oder Seite geändert).", tags=["warning"], priority=3)
                    except Exception as e:  # noqa: BLE001
                        log(f"Warn-Push fehlgeschlagen: {e}")
            else:
                fails[platform] = 0

        # ---- Trefferliste: aktuelle passende Anzeigen von Willhaben und Vinted
        if not dry:
            old_listings = {
                (x.get("platform"), x.get("id")): x
                for x in (state.get("listings") or [])
            }
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            refreshed = []
            for key, item in current_listings.items():
                old = old_listings.get(key, {})
                refreshed.append({**item, "found_at": old.get("found_at", now)})
            state["listings"] = refreshed[:MAX_LISTINGS]

        # ---- Benachrichtigungen
        entries = list(pending.values())
        sent_entries = []
        # Bei genau einem Treffer über dem Limit lieber einzeln senden als "1 weitere"
        limit = MAX_SINGLE_PUSHES if len(entries) > MAX_SINGLE_PUSHES + 1 else len(entries)


        def mark(entry_list):
            for e in entry_list:
                for skey in e["skeys"]:
                    lst = seen_all.setdefault(skey, [])
                    if e["item"]["id"] not in lst:
                        lst.append(e["item"]["id"])

        for e in entries[:limit]:
            it = e["item"]
            title = f"{it['platform'].title()} · {fmt_price(it['price'])}"
            msg = it["title"] + (f"\n{it['extra']}" if it["extra"] else "")
            if dry:
                log(f"[DRY] {title} | {msg!r} | {it['url']}")
                continue
            try:
                push(server, topic, title, msg, click=it["url"], tags=["video_game"])
                sent_entries.append(e)
            except Exception as ex:  # noqa: BLE001
                log(f"Push fehlgeschlagen ({it['id']}): {ex} - wird beim nächsten Lauf erneut versucht")

        rest = entries[limit:]
        if rest:
            lines = [f"{e['item']['title']} ({fmt_price(e['item']['price'])})" for e in rest[:8]]
            if len(rest) > 8:
                lines.append(f"… und {len(rest) - 8} weitere")
            if dry:
                log("[DRY] Sammelnachricht:\n" + "\n".join(lines))
            else:
                try:
                    push(server, topic, f"{len(rest)} weitere neue Anzeigen", "\n".join(lines),
                         click=rest[0]["item"]["url"], tags=["video_game"])
                    sent_entries.extend(rest)
                except Exception as ex:  # noqa: BLE001
                    log(f"Sammel-Push fehlgeschlagen: {ex}")

        if not dry:
            mark(sent_entries)
        log(f"{len(entries)} neue Treffer insgesamt.")
    finally:
        if not dry:
            for k, lst in seen_all.items():
                seen_all[k] = lst[-MAX_SEEN:]
            state["initialized"] = sorted(initialized)
            save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
