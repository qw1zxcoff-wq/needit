import json
import os
import random
import re
import string
import threading
import time
import urllib.error
import urllib.request

TOKEN = os.environ.get("BOT_TOKEN") or "9159943768:AAF7uZuTYZ2QtIcYRSIjF44NDnpUb7pbOoQ"
API = "https://api.telegram.org/bot%s/" % TOKEN

DELAY = 1.5
DEFAULT_N = 20
MAX_N = 40
COOLDOWN = 20

VOWELS = "aeiou"
CONS = "bcdfghjklmnpqrstvwxz"
PATTERNS = ["CVCVC", "CVCCV", "CCVCV", "VCVCV", "CVVCV", "VCCVC"]

busy = set()
last_run = {}

HELP = (
    "Ищу свободные 5-буквенные юзернеймы.\n\n"
    "/find [N] - красивые (читаемые)\n"
    "/random [N] - полностью случайные\n"
    "/check имя - проверить конкретный юз\n\n"
    "N - сколько проверить (до %d, по умолчанию %d).\n"
    "Пометка «свободен?» не гарантия: юз может быть зарезервирован или на Fragment."
    % (MAX_N, DEFAULT_N)
)


def api(method, **params):
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        API + method, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


def send(chat_id, text):
    try:
        api("sendMessage", chat_id=chat_id, text=text)
    except Exception as e:
        print("send error:", e)


def gen_pronounceable():
    p = random.choice(PATTERNS)
    return "".join(random.choice(CONS if c == "C" else VOWELS) for c in p)


def gen_random():
    return "".join(random.choice(string.ascii_lowercase) for _ in range(5))


def check(name):
    """True = похоже свободен, False = занят, None = ошибка/лимит"""
    req = urllib.request.Request(
        "https://t.me/" + name,
        headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(30)
        return None
    except Exception:
        return None
    return "tgme_page_title" not in html


def search(chat_id, gen, n):
    try:
        send(chat_id, "Проверяю %d юзов, подожди ~%d сек..." % (n, int(n * DELAY)))
        found, seen, errors, done = [], set(), 0, 0
        while done < n and errors < 5:
            name = gen()
            if name in seen:
                continue
            seen.add(name)
            res = check(name)
            if res is None:
                errors += 1
                time.sleep(DELAY)
                continue
            errors = 0
            done += 1
            if res:
                found.append("@" + name)
            time.sleep(DELAY)
        if found:
            send(chat_id, "Похоже свободны (%d из %d):\n%s" % (len(found), done, "\n".join(found)))
        else:
            send(chat_id, "Проверено %d, свободных не нашлось. Попробуй ещё раз." % done)
        if errors >= 5:
            send(chat_id, "t.me часто не отвечает, остановился. Попробуй позже.")
    finally:
        busy.discard(chat_id)


def parse_n(parts):
    if len(parts) > 1 and parts[1].isdigit():
        return max(1, min(int(parts[1]), MAX_N))
    return DEFAULT_N


def handle(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()

    if cmd in ("/start", "/help"):
        send(chat_id, HELP)
