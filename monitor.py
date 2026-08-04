#!/usr/bin/env python3
"""
Warsaw flat monitor — Otodom + OLX -> Telegram.

Логика:
  1. Тянет страницы поиска Otodom и OLX с фильтрами в URL.
  2. Достаёт объявления из JSON, встроенного в страницу (надёжнее HTML).
  3. Прогоняет через фильтры повторно (страховка от «слишком широких» URL).
  4. Сравнивает ID с seen.json — новые отправляет в Telegram.
  5. Дописывает новые ID в seen.json (его коммитит GitHub Actions).

Секреты берутся из переменных окружения:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import json
import os
import re
import sys
import time
import html
import urllib.parse

import requests

import config

SEEN_FILE = "seen.json"
OTODOM_BASE = "https://www.otodom.pl"
OLX_SEARCH = "https://www.olx.pl/nieruchomosci/mieszkania/wynajem/warszawa/"


# ------------------------------------------------------------------ helpers

def log(msg):
    print(msg, flush=True)


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, ValueError):
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        # ограничиваем размер: держим последние ~5000 id
        json.dump(sorted(seen)[-5000:], f, ensure_ascii=False, indent=0)


def fetch(url):
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=25)
            if r.status_code == 200:
                return r.text
            log(f"  [{r.status_code}] {url}")
            if r.status_code == 404:
                return None          # 404 не лечится ретраем — сразу пробуем след. вариант
        except requests.RequestException as e:
            log(f"  retry {attempt+1}: {e}")
        time.sleep(3)
    return None


def deep_find_list(obj, predicate):
    """Рекурсивно ищет в JSON первый список, чьи элементы проходят predicate.
    Устойчиво к смене путей во внутренней структуре сайта."""
    found = []

    def walk(node):
        if isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node) and \
               sum(1 for x in node if predicate(x)) >= max(1, len(node) // 2):
                found.append(node)
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)

    walk(obj)
    # возвращаем самый длинный подходящий список
    return max(found, key=len) if found else []


def extract_json_blob(html_text, marker_id="__NEXT_DATA__"):
    """Достаёт JSON из <script id="__NEXT_DATA__">…</script> (Otodom, Next.js)."""
    m = re.search(
        r'<script id="%s"[^>]*>(.*?)</script>' % re.escape(marker_id),
        html_text, re.DOTALL,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


# ------------------------------------------------------------------ Otodom

def otodom_candidate_urls():
    # 2+ комнаты; client-side passes() всё равно перепроверит rooms>=ROOMS_MIN
    rooms = ["TWO", "THREE", "FOUR", "FIVE", "SIX_OR_MORE"] if config.ROOMS_MIN <= 2 \
        else ["THREE", "FOUR", "FIVE", "SIX_OR_MORE"]
    params = {
        "priceMax": config.PRICE_MAX,
        "areaMin": config.AREA_MIN,
        "roomsNumber": "[%s]" % ",".join(rooms),
        "by": "LATEST",
        "direction": "DESC",
        "viewType": "listing",
    }
    if config.OWNERS_ONLY:
        params["ownerTypeSingleSelect"] = "PRIVATE"
    q = urllib.parse.urlencode(params, safe="[],")
    # Город одним запросом; район отсекаем на своей стороне (district_ok).
    # Несколько форматов пути — берём первый, что ответит 200.
    paths = [
        "/pl/oferty/wynajem/mieszkanie/warszawa",
        "/pl/wyniki/wynajem/mieszkanie/mazowieckie/warszawa/warszawa",
        "/pl/oferty/wynajem/mieszkanie/mazowieckie/warszawa/warszawa",
    ]
    return [f"{OTODOM_BASE}{p}?{q}" for p in paths]


def _num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    m = re.search(r"[\d\s.,]+", str(x))
    if not m:
        return None
    s = m.group(0).replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


_ROOMS_MAP = {
    "ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5,
    "SIX": 6, "SIX_OR_MORE": 6, "SEVEN": 7, "EIGHT": 8,
    "NINE": 9, "TEN": 10, "TEN_OR_MORE": 10, "MORE": 10,
}


def parse_otodom():
    listings = []
    page = used = None
    for url in otodom_candidate_urls():
        log(f"Otodom пробую: {url.split('?')[0]}")
        p = fetch(url)
        time.sleep(config.REQUEST_DELAY)
        if p:
            page, used = p, url
            break
    if not page:
        log("  Otodom: ни один вариант URL не ответил 200")
        return []
    data = extract_json_blob(page)
    if not data:
        log("  Otodom: __NEXT_DATA__ не найден — вёрстка изменилась")
        return []
    items = deep_find_list(
        data,
        lambda d: ("id" in d or "slug" in d) and
                  any(k in d for k in ("totalPrice", "price", "rentPrice")),
    )
    log(f"  Otodom: сработал {used.split('?')[0]} — карточек в JSON: {len(items)}")
    if items:
        log("  [debug] ключи 1-й карточки Otodom: " + ", ".join(list(items[0].keys())))
        log("  [debug] 1-я карточка Otodom (обрезано): "
            + json.dumps(items[0], ensure_ascii=False)[:1400])
    for it in items[:config.MAX_ITEMS_PER_PAGE]:
            slug = it.get("slug")
            oid = it.get("id")
            if not slug and not oid:
                continue
            price = _num((it.get("totalPrice") or {}).get("value")
                         if isinstance(it.get("totalPrice"), dict)
                         else it.get("totalPrice") or it.get("price"))
            area = _num(it.get("areaInSquareMeters") or it.get("area"))

            # roomsNumber у Otodom — строка-enum (ONE/TWO/THREE/...)
            rn = it.get("roomsNumber")
            rooms = _ROOMS_MAP.get(rn) if isinstance(rn, str) else _num(rn)

            # Район лежит в location.reverseGeocoding, уровень "district".
            locobj = it.get("location") or {}
            rg = (locobj.get("reverseGeocoding") or {}).get("locations") or []
            district_full = ""
            for e in rg:
                if e.get("locationLevel") == "district":
                    district_full = e.get("fullName") or e.get("name") or ""
                    break
            if not district_full:
                district_full = ", ".join(e.get("name", "") for e in rg if e.get("name"))
            addr = locobj.get("address") or {}
            street = ((addr.get("street") or {}).get("name")) or ""
            location = (street + ", " + district_full).strip(", ") if street else district_full

            # готовый булев флаг собственника
            is_private = bool(it.get("isPrivateOwner"))

            listings.append({
                "id": f"otodom:{oid or slug}",
                "source": "Otodom",
                "title": it.get("title") or "—",
                "price": price,
                "area": area,
                "rooms": rooms,
                "location": location or "Warszawa",
                "is_private": is_private,
                "url": f"{OTODOM_BASE}/pl/oferta/{slug}" if slug
                       else f"{OTODOM_BASE}/pl/oferta/{oid}",
            })
    return listings


# ------------------------------------------------------------------ OLX

def olx_url():
    params = {
        "search[order]": "created_at:desc",
        "search[filter_float_price:to]": config.PRICE_MAX,
        "search[filter_float_m:from]": config.AREA_MIN,
    }
    if config.OWNERS_ONLY:
        params["search[private_business]"] = "private"
    return OLX_SEARCH + "?" + urllib.parse.urlencode(params)


def parse_olx():
    url = olx_url()
    log("OLX: warszawa")
    page = fetch(url)
    time.sleep(config.REQUEST_DELAY)
    if not page:
        return []
    # OLX кладёт состояние в window.__PRERENDERED_STATE__ = "<url-encoded json>";
    m = re.search(r'__PRERENDERED_STATE__\s*=\s*"(.*?)";', page, re.DOTALL)
    data = None
    if m:
        try:
            raw = m.group(1).encode().decode("unicode_escape")
            data = json.loads(urllib.parse.unquote(raw))
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None
    if data is None:
        data = extract_json_blob(page, "olx-init-config") or {}

    items = deep_find_list(
        data,
        lambda d: "id" in d and ("url" in d or "title" in d),
    )
    listings = []
    for it in items[:config.MAX_ITEMS_PER_PAGE]:
        oid = it.get("id")
        title = it.get("title") or "—"
        u = it.get("url") or ""
        if u and not u.startswith("http"):
            u = "https://www.olx.pl" + u
        # цена / площадь / комнаты часто лежат в params[]
        price = area = rooms = None
        loc_txt = ""
        for p in (it.get("params") or []):
            key = (p.get("key") or "").lower()
            val = (p.get("value") or {})
            v = val.get("value") if isinstance(val, dict) else val
            label = val.get("label") if isinstance(val, dict) else None
            if key == "price":
                price = _num((val.get("value") if isinstance(val, dict) else None) or label)
            elif key in ("m", "area"):
                area = _num(v or label)
            elif key in ("rooms",):
                rooms = _num(v or label)
        loc = it.get("location") or {}
        if isinstance(loc, dict):
            loc_txt = (loc.get("city", {}) or {}).get("name", "") + \
                      " " + (loc.get("district", {}) or {}).get("name", "")
        is_private = True
        if isinstance(it.get("business"), bool):
            is_private = not it["business"]
        listings.append({
            "id": f"olx:{oid}",
            "source": "OLX",
            "title": title,
            "price": price,
            "area": area,
            "rooms": rooms,
            "location": loc_txt.strip() or "Warszawa",
            "is_private": is_private,
            "url": u,
        })
    return listings


# ------------------------------------------------------------------ filter

_PL_MAP = str.maketrans("ąćęłńóśźż", "acelnoszz")


def _norm(s):
    return (s or "").lower().translate(_PL_MAP)


def district_ok(location):
    targets = getattr(config, "TARGET_DISTRICTS", [])
    if not targets:
        return True
    loc = _norm(location)
    return any(_norm(d) in loc for d in targets)


def passes(l):
    if not district_ok(l["location"]):
        return False
    if config.OWNERS_ONLY and not l["is_private"]:
        return False
    if l["price"] is not None and l["price"] > config.PRICE_MAX:
        return False
    if l["area"] is not None and l["area"] < config.AREA_MIN:
        return False
    if l["rooms"] is not None and l["rooms"] < config.ROOMS_MIN:
        return False
    return True


# ------------------------------------------------------------------ Telegram

def format_msg(l):
    price = f"{int(l['price'])} zł" if l["price"] else "цена н/д"
    area = f"{int(l['area'])} м²" if l["area"] else "площадь н/д"
    rooms = f"{int(l['rooms'])}-комн." if l["rooms"] else ""
    owner = "👤 собственник" if l["is_private"] else "🏢 агентство"
    title = html.escape(l["title"])[:120]
    loc = html.escape(l["location"])[:80]
    parts = [
        f"🏠 <b>{title}</b>",
        f"💰 {price}   📐 {area}   {rooms}".strip(),
        f"📍 {loc}",
        f"{owner}   •   {l['source']}",
        f"{html.escape(l['url'])}",
    ]
    return "\n".join(parts)


def send_telegram(token, chat_id, text):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    if r.status_code != 200:
        log(f"  Telegram error {r.status_code}: {r.text[:200]}")
    return r.status_code == 200


# ------------------------------------------------------------------ main

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    dry = "--dry-run" in sys.argv
    if not dry and (not token or not chat_id):
        log("Нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID в окружении.")
        sys.exit(1)

    seen = load_seen()
    log(f"Уже видели: {len(seen)} объявлений")

    all_listings = []
    try:
        all_listings += parse_otodom()
    except Exception as e:
        log(f"Otodom упал: {e}")
    try:
        all_listings += parse_olx()
    except Exception as e:
        log(f"OLX упал: {e}")

    log(f"Всего собрано карточек: {len(all_listings)}")

    # --- временная диагностика отсева (убрать после отладки) ---
    from collections import Counter
    tally = Counter()
    for l in all_listings:
        if l["id"] in seen:
            tally["уже видели"] += 1
        elif not district_ok(l["location"]):
            tally[f"район не совпал [{l['source']}]"] += 1
        elif config.OWNERS_ONLY and not l["is_private"]:
            tally[f"агентство [{l['source']}]"] += 1
        elif l["price"] is not None and l["price"] > config.PRICE_MAX:
            tally["цена выше"] += 1
        elif l["area"] is not None and l["area"] < config.AREA_MIN:
            tally["площадь меньше"] += 1
        elif l["rooms"] is not None and l["rooms"] < config.ROOMS_MIN:
            tally["мало комнат"] += 1
        else:
            tally[f"ПРОШЛО [{l['source']}]"] += 1
    log("Диагностика отсева: " + repr(dict(tally)))
    otd = [l for l in all_listings if l["source"] == "Otodom"]
    for l in otd[:3]:
        log(f"  [debug Otodom] loc={l['location']!r} price={l['price']} "
            f"area={l['area']} rooms={l['rooms']} private={l['is_private']}")
    # --- конец диагностики ---

    fresh = [l for l in all_listings if l["id"] not in seen and passes(l)]
    log(f"Новых подходящих: {len(fresh)}")

    sent = 0
    for l in fresh:
        if dry:
            log("--- DRY RUN ---\n" + format_msg(l) + "\n")
        else:
            if send_telegram(token, chat_id, format_msg(l)):
                sent += 1
                time.sleep(1.5)  # не спамим Telegram API
        seen.add(l["id"])

    save_seen(seen)
    log(f"Отправлено: {sent}. seen.json обновлён ({len(seen)} id).")


if __name__ == "__main__":
    main()
