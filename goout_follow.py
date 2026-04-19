#!/usr/bin/env python3
"""
GoOut Artist Follower
Načte CSV se jmény umělců, vyhledá je na GoOut a automaticky je followuje.

Použití:
  python3 goout_follow.py

Před spuštěním:
  1. Přihlas se na goout.net v prohlížeči
  2. Zkopíruj cookies (viz instrukce v README nebo níže)
  3. Vlož je do souboru cookies.json nebo je zadej interaktivně
"""

import csv
import json
import time
import random
import logging
import sys
from typing import Optional

try:
    import requests
except ImportError:
    print("Chybí balíček 'requests'. Instaluj: pip3 install requests")
    sys.exit(1)

# ── Konfigurace ────────────────────────────────────────────────────────────────

INPUT_CSV    = "artists.csv"          # Vstupní CSV soubor
OUTPUT_CSV   = "results.csv"          # Výstupní CSV s výsledky
COOKIES_FILE = "cookies.json"         # Soubor s cookies (volitelné)
LOG_FILE     = "goout_follow.log"     # Log soubor

RATE_LIMIT_MIN = 1.2   # Minimální čekání mezi requesty (sekundy)
RATE_LIMIT_MAX = 2.5   # Maximální čekání mezi requesty (sekundy)
DRY_RUN = False        # True = jen simulace, nefolowuje doopravdy

BASE_URL         = "https://goout.net"
SEARCH_ENDPOINT  = f"{BASE_URL}/services/entities/v1/performers"
FOLLOWERS_ENDPOINT = f"{BASE_URL}/services/social/v1/followers"
# Primární follow endpoint (Nuxt/v2)
FOLLOW_V2_ENDPOINT = f"{BASE_URL}/services/social/follow/v2/follow"
# Záložní follow endpoint (starší)
FOLLOW_V1_ENDPOINT = f"{BASE_URL}/services/social/v1/follow/liked"
AUTH_ENDPOINT    = f"{BASE_URL}/services/user/auth/v1/state"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "cs,en;q=0.9",
    "Origin": "https://goout.net",
    "Referer": "https://goout.net/en/artists/",
}

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8", mode="a"),
        logging.StreamHandler(sys.stdout),
    ],
    force=True,
)
log = logging.getLogger(__name__)

# ── Cookies ───────────────────────────────────────────────────────────────────

def load_cookies_from_file(path: str) -> dict:
    """Načte cookies ze souboru cookies.json."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Podporuje formáty: dict, list[{name, value}], list[{name, value, ...}]
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {item["name"]: item["value"] for item in data if "name" in item}
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"Nepodařilo se načíst {path}: {e}")
    return {}


def cookies_from_string(cookie_str: str) -> dict:
    """Parsuje cookies ze stringu ve formátu 'name=value; name2=value2'."""
    cookies = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()
    return cookies


def prompt_for_cookies() -> dict:
    """Interaktivně požádá uživatele o cookies."""
    print("\n" + "="*60)
    print("JAK ZÍSKAT COOKIES:")
    print("="*60)
    print("1. Otevři Chrome/Firefox a přihlas se na https://goout.net")
    print("2. Stiskni F12 → záložka 'Application' (Chrome) nebo 'Storage' (Firefox)")
    print("3. V levém panelu: Cookies → https://goout.net")
    print("4. Zkopíruj hodnotu cookie '_goout_session' (nebo 'token'/'auth')")
    print("")
    print("NEBO:")
    print("1. Na goout.net stiskni F12 → záložka 'Network'")
    print("2. Refreshni stránku, klikni na první request")
    print("3. V Headers najdi 'Cookie:' a zkopíruj celý řádek za 'Cookie: '")
    print("="*60)
    print("")
    raw = input("Vlož celý Cookie string (nebo Enter pro přeskočení): ").strip()
    if not raw:
        log.warning("Cookies nezadány – follow akce selžou (nejsi přihlášen/a).")
        return {}
    return cookies_from_string(raw)


def get_cookies() -> dict:
    """Načte cookies ze souboru nebo se zeptá uživatele."""
    cookies = load_cookies_from_file(COOKIES_FILE)
    if cookies:
        log.info(f"Načteny cookies ze souboru {COOKIES_FILE} ({len(cookies)} cookies)")
        return cookies
    log.info(f"Soubor {COOKIES_FILE} nenalezen.")
    return prompt_for_cookies()

# ── API volání ────────────────────────────────────────────────────────────────

def check_auth(session: requests.Session) -> tuple[bool, str]:
    """
    Ověří přihlášení. Vrací (authenticated, userId).
    Také nastaví Authorization header ze session cookies.
    """
    # Přidej Bearer token z accessToken cookie
    access_token = session.cookies.get("accessToken", "")
    if access_token:
        session.headers["Authorization"] = f"Bearer {access_token}"

    try:
        r = session.get(AUTH_ENDPOINT, timeout=10)
        data = r.json()
        if data.get("authenticated"):
            user = data.get("userData", {}) or {}
            user_id = str(user.get("userId", ""))
            log.info(f"✅ Přihlášen/a jako: {user.get('firstName')} {user.get('lastName')} "
                     f"({user.get('email')})  [ID: {user_id}]")
            return True, user_id
        else:
            log.warning("❌ Nejsi přihlášen/a! Follow akce nebudou fungovat.")
            return False, ""
    except Exception as e:
        log.warning(f"Nepodařilo se ověřit auth: {e}")
        return False, ""


def search_performer(session: requests.Session, name: str) -> Optional[dict]:
    """
    Vyhledá performera podle jména.
    Vrací první přesnou nebo nejlepší shodu, nebo None.
    """
    params = {
        "languages[]": "en",
        "listInvisible": "false",
        "include": "images",
        "excludeCategories[]": "organizer",
        "query": name,
    }
    try:
        r = session.get(SEARCH_ENDPOINT, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        performers = data.get("performers", [])

        if not performers:
            return None

        # Hledáme přesnou shodu (case-insensitive)
        name_lower = name.lower()
        for p in performers:
            en_name = p.get("locales", {}).get("en", {}).get("name", "")
            if en_name.lower() == name_lower:
                return p

        # Jinak první výsledek jako nejlepší shodu
        return performers[0]

    except requests.HTTPError as e:
        log.error(f"HTTP chyba při hledání '{name}': {e}")
    except Exception as e:
        log.error(f"Chyba při hledání '{name}': {e}")
    return None


def follow_performer(session: requests.Session, performer_id: int, dry_run: bool = False) -> bool:
    """
    Followuje performera přes v2 endpoint.
    Vrací True při úspěchu.
    """
    if dry_run:
        log.info(f"  [DRY RUN] Followuji performer ID {performer_id}")
        return True

    try:
        r = session.post(
            FOLLOW_V2_ENDPOINT,
            data={"ids": performer_id, "type": "performer", "action": "like"},
            timeout=15,
        )
        if r.status_code in (200, 201, 204):
            data = r.json() if r.text else {}
            inner_status = data.get("status", 200)
            if inner_status not in (401, 403):
                return True
            log.warning(f"  Follow odmítnut ({inner_status}): {data.get('message', '')}")
            return False
        log.warning(f"  Follow vrátil HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        log.error(f"  Chyba při follow performer {performer_id}: {e}")
        return False


def check_already_following(
    session: requests.Session, performer_ids: list[int], user_id: str = ""
) -> set[int]:
    """
    Vrátí sadu ID performerů, které již followuješ.
    Musí se předat user_id pro správné vrácení likeState.
    """
    if not performer_ids:
        return set()
    params = [("performerIds[]", pid) for pid in performer_ids]
    params.append(("imageCount", "0"))
    if user_id:
        params.append(("userId", user_id))
    try:
        r = session.get(FOLLOWERS_ENDPOINT, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        # Odpověď: {"followers": [{"attributes": {"likeState": "LIKE|null"}, ...}]}
        followed = set()
        for f in data.get("followers", []):
            like_state = f.get("attributes", {}).get("likeState")
            if like_state:  # "LIKE" nebo jiná non-null hodnota
                performer_rel = f.get("relationships", {}).get("performer", {})
                pid = performer_rel.get("id")
                if pid:
                    followed.add(int(pid))
        return followed
    except Exception as e:
        log.warning(f"Nepodařilo se zkontrolovat follow status: {e}")
        return set()

# ── Hlavní logika ─────────────────────────────────────────────────────────────

def load_artists(csv_path: str) -> list[str]:
    """Načte jména umělců z CSV souboru."""
    artists = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("Artist", "").strip()
            if name:
                artists.append(name)
    log.info(f"Načteno {len(artists)} umělců z {csv_path}")
    return artists


def save_results(results: list[dict], csv_path: str):
    """Uloží výsledky do CSV."""
    fieldnames = ["artist", "status", "performer_id", "performer_name", "performer_url", "note"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    log.info(f"Výsledky uloženy do {csv_path}")


def rate_limit():
    """Čeká náhodnou dobu pro rate limiting."""
    wait = random.uniform(RATE_LIMIT_MIN, RATE_LIMIT_MAX)
    time.sleep(wait)


def main():
    log.info("="*60)
    log.info("GoOut Artist Follower – start")
    log.info(f"DRY_RUN = {DRY_RUN}")
    log.info("="*60)

    # Načti cookies
    cookies = get_cookies()

    # Vytvoř session
    session = requests.Session()
    session.headers.update(HEADERS)
    if cookies:
        session.cookies.update(cookies)

    # Ověř přihlášení
    is_authenticated, user_id = check_auth(session)
    dry_run = DRY_RUN
    if not is_authenticated and not dry_run:
        print("\n⚠️  Nejsi přihlášen/a. Chceš pokračovat v DRY RUN módu? (y/n): ", end="")
        choice = input().strip().lower()
        if choice == "y":
            dry_run = True
            log.info("Přepnuto do DRY RUN módu.")
        else:
            log.info("Ukončeno. Přihlas se nejdřív a zadej cookies.")
            sys.exit(0)

    # Načti umělce
    artists = load_artists(INPUT_CSV)

    results = []
    stats = {"found": 0, "not_found": 0, "followed": 0, "error": 0}

    for i, artist_name in enumerate(artists, 1):
        log.info(f"[{i}/{len(artists)}] Hledám: {artist_name}")

        # Vyhledej performera
        performer = search_performer(session, artist_name)
        rate_limit()

        if performer is None:
            log.info(f"  ❌ Nenalezen: {artist_name}")
            stats["not_found"] += 1
            results.append({
                "artist": artist_name,
                "status": "not_found",
                "performer_id": "",
                "performer_name": "",
                "performer_url": "",
                "note": "",
            })
            continue

        performer_id   = performer["id"]
        en_locale      = performer.get("locales", {}).get("en", {})
        performer_name = en_locale.get("name", "")
        performer_url  = en_locale.get("siteUrl") or performer.get("url", "")
        stats["found"] += 1

        log.info(f"  ✅ Nalezen: {performer_name} (ID: {performer_id})")

        # Followuj (API je idempotentní – opakovaný follow nevadí)
        success = follow_performer(session, performer_id, dry_run=dry_run)
        rate_limit()

        if success:
            log.info(f"  💚 Followováno!")
            stats["followed"] += 1
            results.append({
                "artist": artist_name,
                "status": "followed",
                "performer_id": performer_id,
                "performer_name": performer_name,
                "performer_url": performer_url,
                "note": "dry_run" if dry_run else "",
            })
        else:
            log.warning(f"  ⚠️  Follow selhal")
            stats["error"] += 1
            results.append({
                "artist": artist_name,
                "status": "follow_failed",
                "performer_id": performer_id,
                "performer_name": performer_name,
                "performer_url": performer_url,
                "note": "",
            })

        # Průběžně ukládej každých 50 umělců
        if i % 50 == 0:
            save_results(results, OUTPUT_CSV)
            log.info(f"--- Průběžný stav: {stats} ---")

    # Ulož finální výsledky
    save_results(results, OUTPUT_CSV)

    # Souhrn
    log.info("")
    log.info("="*60)
    log.info("HOTOVO – SOUHRN")
    log.info(f"  Celkem umělců:    {len(artists)}")
    log.info(f"  Nalezeno:         {stats['found']}")
    log.info(f"  Nenalezeno:       {stats['not_found']}")
    log.info(f"  Followováno:      {stats['followed']}")
    log.info(f"  Chyby:            {stats['error']}")
    log.info("="*60)


if __name__ == "__main__":
    main()
