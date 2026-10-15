"""
Монитор новых объявлений об аренде в Белграде (Halo Oglasi, 4zida, nekretnine.rs).
Каждый запуск: берёт первые страницы выдачи (сортировка «сначала новые»),
находит объявления, которых ещё не было в state.json, открывает только их,
фильтрует по критериям и шлёт подходящие в Telegram.

Переменные окружения: TG_TOKEN, TG_CHAT_ID.
"""
import json
import os
import re
import sys
import time
import html as htmllib
from pathlib import Path

import requests
from bs4 import BeautifulSoup

STATE_FILE = Path(__file__).with_name("state.json")
MAX_SEEN_PER_SOURCE = 3000
MAX_DETAILS_PER_SOURCE = 20      # сколько новых объявлений максимум открываем за запуск
PAGES = 2                        # сколько страниц выдачи смотрим
FAIL_ALERT_AFTER = 6             # после стольких подряд неудачных запусков — предупреждение в Telegram

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "sr,en;q=0.8,ru;q=0.6"})

# ---------------- критерии ----------------
MAX_PRICE = 1100
MIN_M2 = 60

DUPLEX_RE = re.compile(
    r"dupleks|duplex|dva nivoa|dve eta[žz]e|dvoeta[žz]n|na dva sprata|galerij(?!am|e |a )|"
    r"tri nivoa|tripleks|triplex|troeta[žz]|unutra[šs]nj\w* stepeni|gornj\w* nivo|donj\w* nivo|"
    r"na oba sprata|prizemlje i sprat|vertikal|u nizu|niz ku[ćc]|u[šs]oren|na spratu su",
    re.I)

BAD_AREA_RE = re.compile(
    r"[čc]ukarica|rakovica|[žz]arkovo|banovo|cerak|julino|ko[šs]utnjak|[žz]eleznik|sremčica|sremcica|"
    r"umka|ostru[žz]nica|resnik|kne[žz]evac|sur[čc]in|dobanovci|ugrinovci|batajnica|bor[čc]a|ov[čc]a|"
    r"krnja[čc]a|grocka|vin[čc]a|ritopek|le[šs]tane|kalu[đd]j?erica|bole[čc]|obrenovac|barajevo|sopot|"
    r"mladenovac|lazarevac|ripanj|zuce|beli[ -]potok|jajinci|altina|galenika|zemun[ -]polje|kumodra[žz]|"
    r"filmski|vidikovac|labudovo|kanarevo|miljakovac|petlovo|skojevsk|bele[ -]vode|golf|"
    r"sun[čc]ana[ -]padina|kijevo|pinosava|vr[čc]in|mo[šs]tanica|padinsk|ledine",
    re.I)

NO_PETS_RE = re.compile(
    r"(nisu dozvolj\w*|nije dozvolj\w*|zabranjen\w*|bez)\s+(ku[ćc]n\w*\s+)?(ljubim\w*|[žz]ivotinj\w*)|"
    r"(ljubim\w*|[žz]ivotinj\w*)\s+(nisu|ne)\s+(dozvolj\w*|dolaze)|nije pet[ -]?friendly",
    re.I)
PETS_OK_RE = re.compile(r"ljubim\w*.{0,25}(dozvolj|dobrodo[šs]|mo[žz]e|mogu[ćc])|pet[ -]?friendly", re.I)
WORKERS_RE = re.compile(r"radnik|radnic|hostel|kolektivn", re.I)
SINGLE_LEVEL_RE = re.compile(
    r"prizemn\w* ku[ćc]|ku[ćc]a je prizemna|jednoeta[žz]n|sprat ku[ćc]e|prizemlje ku[ćc]e|"
    r"deo ku[ćc]e|ceo sprat", re.I)
COMMERCIAL_RE = re.compile(r"\blokal\b|poslovni prostor|kancelarij", re.I)
FOREIGNERS_NO_RE = re.compile(r"nije za strance|ne izdaje\w* strancima|samo (za )?(dr[žz]avljan|srb)", re.I)
EMPTY_RE = re.compile(r"\bprazan\b|\bprazno\b|nenamešten|nenamesten", re.I)


# ---------------- утилиты ----------------
def get(url, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, timeout=25)
            if r.status_code == 200 and len(r.text) > 2000:
                return r.text
            print(f"  ! {url} -> HTTP {r.status_code}, {len(r.text)} bytes", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  ! {url} -> {e}", file=sys.stderr)
        time.sleep(3 * (i + 1))
    return None


def tg(text):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        print("[tg disabled]\n" + text)
        return
    try:
        S.post(f"https://api.telegram.org/bot{token}/sendMessage",
               data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                     "disable_web_page_preview": "false"}, timeout=20)
    except requests.RequestException as e:
        print("telegram error:", e, file=sys.stderr)
    time.sleep(1)


def esc(s):
    return htmllib.escape(str(s or ""), quote=False)


def num(x):
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(str(x).replace(".", "").replace(",", ".")) if x not in (None, "") else None
    except ValueError:
        m = re.search(r"\d[\d.]*", str(x))
        return float(m.group(0).replace(".", "")) if m else None


# ---------------- Halo Oglasi ----------------
HALO_SEARCHES = [
    ("halo-stanovi", "flat",
     "https://www.halooglasi.com/nekretnine/izdavanje-stanova/beograd?kvadratura_d_from=60&kvadratura_d_unit=1"
     "&cena_d_to=1100&cena_d_unit=4&sort=ValidFromForDisplay&order=desc&page={p}"),
    ("halo-kuce", "house",
     "https://www.halooglasi.com/nekretnine/izdavanje-kuca/beograd?kvadratura_d_from=60&kvadratura_d_unit=1"
     "&cena_d_to=1100&cena_d_unit=4&sort=ValidFromForDisplay&order=desc&page={p}"),
]


def halo_list(url_tpl):
    out = {}
    for p in range(1, PAGES + 1):
        h = get(url_tpl.format(p=p))
        if h is None:
            return None if p == 1 else out
        soup = BeautifulSoup(h, "html.parser")
        for it in soup.select(".product-item"):
            a = it.select_one("h3 a")
            if not a or not a.get("href"):
                continue
            href = a["href"].split("?")[0]
            m = re.search(r"/(\d{10,})$", href)
            if m:
                out[m.group(1)] = "https://www.halooglasi.com" + href
        time.sleep(1.5)
    return out


def halo_detail(url):
    h = get(url)
    if not h:
        return None
    i = h.find("CurrentClassified=")
    if i < 0:
        return None
    s = h.find("{", i)
    dep = 0
    for j in range(s, len(h)):
        if h[j] == "{":
            dep += 1
        elif h[j] == "}":
            dep -= 1
            if dep == 0:
                break
    try:
        o = json.loads(h[s:j + 1])
    except json.JSONDecodeError:
        return None
    f = o.get("OtherFields") or {}
    text = BeautifulSoup(o.get("TextHtml") or "", "html.parser").get_text(" ")
    loc = " / ".join(x for x in [f.get("lokacija_s"), f.get("mikrolokacija_s"), f.get("ulica_t")] if x)
    return {
        "title": o.get("Title", ""),
        "text": text,
        "m2": f.get("kvadratura_d"),
        "price": f.get("cena_d"),
        "loc": loc.replace("Opština ", ""),
        "heating": f.get("grejanje_s", ""),
        "furn": f.get("namestenost_s", ""),
        "adv": f.get("oglasivac_nekretnine_s", ""),
        "tags": " ".join(f.get("dodatno_ss") or []),
        "state": o.get("StateId"),
    }


# ---------------- 4zida ----------------
ZIDA_SEARCHES = [
    ("4zida-stanovi", "flat", "https://www.4zida.rs/izdavanje-stanova/beograd/do-1100-evra?sortiranje=najnoviji&strana={p}"),
    ("4zida-kuce", "house", "https://www.4zida.rs/izdavanje-kuca/beograd/do-1100-evra?sortiranje=najnoviji&strana={p}"),
]

ZIDA_AD_RE = re.compile(r'"m2":(\d+)[^{}]*?"id":"([0-9a-f]{24})"([^{}]*?)"price":(\d+)')
ZIDA_URL_RE = re.compile(r'"adWebUrl":"([^"]+/([0-9a-f]{24}))"')


def zida_list(url_tpl):
    out = {}
    for p in range(1, PAGES + 1):
        h = get(url_tpl.format(p=p))
        if h is None:
            return None if p == 1 else out
        h = h.replace('\\"', '"')
        meta = {}
        for m in ZIDA_AD_RE.finditer(h):
            addr = re.search(r'"address":"([^"]*)"', m.group(3))
            meta[m.group(2)] = {"m2": int(m.group(1)), "price": int(m.group(4)),
                                "addr": addr.group(1) if addr else ""}
        for m in ZIDA_URL_RE.finditer(h):
            if m.group(2) in meta:
                meta[m.group(2)]["url"] = "https://www.4zida.rs" + m.group(1)
        for k, v in meta.items():
            if "url" in v:
                out[k] = v
        time.sleep(1.5)
    return out


def zida_detail(info):
    h = get(info["url"])
    if not h:
        return None
    md = re.search(r'<meta name="description" content="([^"]*)"', h)
    md = htmllib.unescape(md.group(1)) if md else ""
    t = re.search(r"<title>([^<]*)", h)
    plain = h.replace("\\n", " ").replace('\\"', '"').replace("\\", "")
    k = plain.find('"text":"')
    desc = plain[k + 8: plain.find('"}', k)] if k >= 0 else ""
    slug = info["url"].split("/")[4] if info["url"].count("/") >= 4 else ""
    return {
        "title": htmllib.unescape(t.group(1)) if t else "",
        "text": md + " " + desc,
        "m2": info.get("m2"),
        "price": info.get("price"),
        "loc": (info.get("addr") or "") + " · " + slug.replace("-", " "),
        "heating": "",
        "furn": "prazno" if "Prazno" in md else ("polunamešteno" if "Polunamešteno" in md else
                                                  ("namešteno" if "Namešteno" in md else "")),
        "adv": "",
        "tags": "Dozvoljeni kućni ljubimci" if "Dozvoljeni kućni ljubimci" in plain else "",
        "inactive": '"active":false' in plain,
    }


# ---------------- nekretnine.rs ----------------
NEK_SEARCHES = [
    ("nek-stanovi", "flat", "https://www.nekretnine.rs/izdavanje-stanova/beograd/"
     "?prezzoMassimo=1100&superficieMinima=60&criterio=data&ordine=desc&pag={p}"),
    ("nek-kuce", "house", "https://www.nekretnine.rs/izdavanje-samostalnih-kuca/beograd/"
     "?prezzoMassimo=1100&superficieMinima=60&criterio=data&ordine=desc&pag={p}"),
]


def nek_list(url_tpl):
    out = {}
    for p in range(1, PAGES + 1):
        h = get(url_tpl.format(p=p))
        if h is None:
            return None if p == 1 else out
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
        if not m:
            return None if p == 1 else out
        try:
            data = json.loads(m.group(1))
            res = data["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]["results"] or []
        except (KeyError, IndexError, json.JSONDecodeError):
            return None if p == 1 else out
        for x in res:
            re_ = x.get("realEstate", x)
            rid = str(re_.get("id") or x.get("id"))
            pr = (re_.get("properties") or [{}])[0]
            loc = pr.get("location") or {}
            out[rid] = {
                "url": f"https://www.nekretnine.rs/oglasi/{rid}/",
                "title": re_.get("title", ""),
                "m2": num(re.sub(r"[^\d,.]", "", str(pr.get("surface", "")))),
                "price": num(re.sub(r"[^\d]", "", str((re_.get("price") or {}).get("formattedValue", "")))),
                "loc": " / ".join(v for v in [loc.get("macrozone"), loc.get("microzone")] if v),
            }
        time.sleep(1.5)
    return out


def nek_detail(info):
    h = get(info["url"])
    if not h:
        return None
    txt = re.sub(r"\s+", " ", BeautifulSoup(h, "html.parser").get_text(" "))
    i = txt.find("Opis")
    k = txt.find("Karakteristike", i)
    desc = txt[i: k if k > i else i + 3000] if i >= 0 else ""
    kar = txt[k: k + 1200] if k >= 0 else ""
    g = re.search(r"Grejanje\s+(.{0,40}?)\s+(Klima|Druge|Energet|Informacije)", kar)
    return {
        "title": info["title"], "text": desc, "m2": info["m2"], "price": info["price"],
        "loc": info["loc"], "heating": g.group(1) if g else "",
        "furn": "", "adv": "", "tags": "",
    }


# ---------------- оценка и сообщение ----------------
def evaluate(kind, d, url):
    """Возвращает (подходит?, список пометок)."""
    blob = " ".join([d.get("title", ""), d.get("text", ""), d.get("tags", "")])
    where = " ".join([d.get("loc", ""), url, d.get("title", "")])
    notes = []
    m2, price = num(d.get("m2")), num(d.get("price"))
    if m2 is not None and m2 < MIN_M2:
        return False, ["< 60 м²"]
    if price is not None and price > MAX_PRICE:
        return False, ["> 1100 €"]
    if BAD_AREA_RE.search(where):
        return False, ["исключённый район"]
    if d.get("inactive") or d.get("state") == 111:
        return False, ["снято"]
    if NO_PETS_RE.search(blob):
        return False, ["животные запрещены"]
    if FOREIGNERS_NO_RE.search(blob):
        return False, ["не для иностранцев"]
    if WORKERS_RE.search(blob):
        return False, ["для рабочих"]
    if COMMERCIAL_RE.search(d.get("title", "")):
        return False, ["коммерческое"]
    dm = DUPLEX_RE.search(blob)
    if kind == "flat" and not dm and "Duplex" not in d.get("tags", ""):
        return False, ["не дуплекс"]
    if dm:
        notes.append(f"«{dm.group(0)}»")
    if kind == "house" and SINGLE_LEVEL_RE.search(blob):
        notes.append("⚠ возможно один уровень/этаж")
    if PETS_OK_RE.search(blob) or "ljubimci" in d.get("tags", "").lower():
        notes.append("🐾 животные можно")
    furn = (d.get("furn") or "").lower()
    if furn == "prazno" or (not furn and EMPTY_RE.search(blob)):
        notes.append("⚠ пустая? (проверить фото)")
    heat = (d.get("heating") or "").lower()
    if re.search(r"\beg\b|struj|elektr|ta pe|klima", heat) or re.search(r"grejanje.{0,20}(na struju|klim)", blob, re.I):
        notes.append("🔌 отопление электр./TA/клима")
    if "atik" in blob.lower() or "potkrovl" in blob.lower():
        notes.append("мансарда/potkrovlje — проверить")
    return True, notes


def message(src, kind, d, url, notes):
    price = num(d.get("price"))
    m2 = num(d.get("m2"))
    lines = [
        f"🏠 <b>{esc(src)}</b> · {'дом' if kind == 'house' else 'квартира'}",
        f"<b>{esc(d.get('title'))[:120]}</b>",
        f"📍 {esc(d.get('loc'))}",
        f"💶 {int(price) if price else '?'} € · {int(m2) if m2 else '?'} м²"
        + (f" · {esc(d.get('furn'))}" if d.get("furn") else "")
        + (f" · {esc(d.get('adv'))}" if d.get("adv") else ""),
    ]
    if d.get("heating"):
        lines.append(f"🔥 {esc(d.get('heating'))}")
    if notes:
        lines.append(" ".join(esc(n) for n in notes))
    lines.append(url)
    return "\n".join(lines)


# ---------------- основной цикл ----------------
def main():
    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    seen = state.setdefault("seen", {})
    fails = state.setdefault("fails", {})
    seeded_now = []
    sent = 0

    sources = ([(n, k, u, halo_list, "halo") for n, k, u in HALO_SEARCHES]
               + [(n, k, u, zida_list, "zida") for n, k, u in ZIDA_SEARCHES]
               + [(n, k, u, nek_list, "nek") for n, k, u in NEK_SEARCHES])

    for name, kind, url_tpl, lister, site in sources:
        print(f"== {name}")
        listing = lister(url_tpl)
        if listing is None or len(listing) == 0:
            fails[name] = fails.get(name, 0) + 1
            print(f"  source failed ({fails[name]} in a row)")
            if fails[name] == FAIL_ALERT_AFTER:
                tg(f"⚠️ Монитор: источник <b>{esc(name)}</b> не отвечает уже {FAIL_ALERT_AFTER} запусков подряд "
                   f"(возможно, сайт блокирует GitHub или поменял вёрстку).")
            continue
        if fails.get(name, 0) >= FAIL_ALERT_AFTER:
            tg(f"✅ Монитор: источник <b>{esc(name)}</b> снова работает.")
        fails[name] = 0

        known = set(seen.get(name, []))
        first_run = name not in seen
        new_ids = [i for i in listing if i not in known]
        print(f"  {len(listing)} on page(s), {len(new_ids)} new")

        if first_run:
            seeded_now.append(f"{name}: {len(listing)}")
        else:
            for rid in new_ids[:MAX_DETAILS_PER_SOURCE]:
                info = listing[rid]
                url = info if isinstance(info, str) else info["url"]
                try:
                    if site == "halo":
                        d = halo_detail(url)
                    elif site == "zida":
                        # быстрый префильтр по данным из выдачи
                        if info.get("m2", 0) < MIN_M2 or BAD_AREA_RE.search(url):
                            continue
                        d = zida_detail(info)
                    else:
                        if (info.get("m2") or 0) and info["m2"] < MIN_M2:
                            continue
                        if BAD_AREA_RE.search(info.get("loc", "") + " " + info.get("title", "")):
                            continue
                        d = nek_detail(info)
                except Exception as e:  # не роняем весь запуск из-за одного объявления
                    print(f"  ! detail {url}: {e}", file=sys.stderr)
                    d = None
                time.sleep(1.5)
                if not d:
                    continue
                ok, notes = evaluate(kind, d, url)
                print(f"  {'+' if ok else '-'} {url} {notes}")
                if ok:
                    tg(message(name, kind, d, url, notes))
                    sent += 1

        merged = list(listing.keys()) + [i for i in seen.get(name, []) if i not in listing]
        seen[name] = merged[:MAX_SEEN_PER_SOURCE]

    if seeded_now:
        tg("🤖 Монитор аренды запущен. Запомнил текущие объявления (о них писать не буду):\n"
           + "\n".join(esc(s) for s in seeded_now)
           + "\nДальше буду присылать только новые подходящие.")

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=0), "utf-8")
    print(f"done, sent {sent}")


if __name__ == "__main__":
    main()
