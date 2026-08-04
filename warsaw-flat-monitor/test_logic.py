"""Оффлайн-тест логики: мок-страницы вместо реальных запросов."""
import json, re, urllib.parse
import monitor, config

# ---- 1. deep_find_list ----
mock_next = {
    "props": {"pageProps": {"data": {"searchAds": {"items": [
        {"id": 101, "slug": "ladne-2-pok-wola-ID101", "title": "2 pok Wola",
         "totalPrice": {"value": 5200}, "areaInSquareMeters": 55, "roomsNumber": 2,
         "locationLabel": {"value": "Wola, Warszawa"}, "ownerType": "private", "agency": None},
        {"id": 102, "slug": "3-pok-srodmiescie-ID102", "title": "3 pok Śródmieście",
         "totalPrice": {"value": 8000}, "areaInSquareMeters": 70, "roomsNumber": 3,
         "locationLabel": {"value": "Śródmieście"}, "ownerType": "agency",
         "agency": {"name": "SuperNieruchomości"}},
        {"id": 103, "slug": "kawalerka-ID103", "title": "Kawalerka",
         "totalPrice": {"value": 3000}, "areaInSquareMeters": 28, "roomsNumber": 1,
         "locationLabel": {"value": "Wola"}, "ownerType": "private", "agency": None},
    ]}}}}
}
found = monitor.deep_find_list(
    mock_next,
    lambda d: ("id" in d or "slug" in d) and any(k in d for k in ("totalPrice","price","rentPrice")))
assert len(found) == 3, found
print("1. deep_find_list нашёл", len(found), "карточек — OK")

# ---- 2. Otodom parse (мок fetch) ----
mock_html_otodom = '<html><script id="__NEXT_DATA__" type="application/json">%s</script></html>' % json.dumps(mock_next)
monitor.fetch = lambda url: mock_html_otodom      # подменяем сеть
config.OTODOM_DISTRICTS = ["wola"]
config.REQUEST_DELAY = 0
otodom_res = monitor.parse_otodom()
print("2. Otodom распарсил:", len(otodom_res), "карточек")
for l in otodom_res:
    print("   ", l["id"], l["price"], l["area"], l["rooms"], "private=" + str(l["is_private"]))

# ---- 3. фильтры ----
passed = [l for l in otodom_res if monitor.passes(l)]
print("3. После фильтров осталось:", len(passed), "(ждём 1 — только 55м²/2пок/собственник/5200)")
assert len(passed) == 1 and passed[0]["id"] == "otodom:101", passed

# ---- 4. OLX parse (мок __PRERENDERED_STATE__) ----
olx_state = {"listing": {"listing": {"ads": [
    {"id": 555, "title": "2 pok Żoliborz", "url": "/d/oferta/2pok-ID555.html",
     "business": False,
     "params": [{"key": "price", "value": {"value": 4800, "label": "4800 zł"}},
                {"key": "m", "value": {"value": 52, "label": "52 m²"}},
                {"key": "rooms", "value": {"value": "2", "label": "2 pokoje"}}],
     "location": {"city": {"name": "Warszawa"}, "district": {"name": "Żoliborz"}}},
]}}}
enc = urllib.parse.quote(json.dumps(olx_state))
mock_html_olx = '<script>window.__PRERENDERED_STATE__ = "%s";</script>' % enc.replace('"','\\"')
monitor.fetch = lambda url: mock_html_olx
olx_res = monitor.parse_olx()
print("4. OLX распарсил:", len(olx_res), "карточек")
for l in olx_res:
    print("   ", l["id"], l["price"], l["area"], l["rooms"], "private=" + str(l["is_private"]), l["url"])
assert len(olx_res) == 1 and olx_res[0]["price"] == 4800, olx_res

# ---- 5. дедупликация ----
seen = {"otodom:101"}
combined = otodom_res + olx_res
fresh = [l for l in combined if l["id"] not in seen and monitor.passes(l)]
print("5. Новых после дедупа (101 уже видели):", [l["id"] for l in fresh])
assert "otodom:101" not in [l["id"] for l in fresh]

# ---- 6. формат сообщения ----
print("6. Пример сообщения в Telegram:\n" + "-"*40)
print(monitor.format_msg(olx_res[0]))
print("-"*40)
print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ ✅")
