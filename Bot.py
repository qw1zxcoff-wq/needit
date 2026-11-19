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
PATTERNS = {
    5: ["CVCVC", "CVCCV", "CCVCV", "VCVCV", "CVVCV", "VCCVC"],
    6: ["CVCVCV", "CVCCVC", "CCVCVC", "VCVCVC", "CVVCVC", "VCCVCV"],
}

KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "5 букв · красивые", "callback_data": "find:5"},
            {"text": "6 букв · красивые", "callback_data": "find:6"},
        ],
        [
            {"text": "5 букв · случайные", "callback_data": "rand:5"},
            {"text": "6 букв · случайные", "callback_data": "rand:6"},
        ],
    ]
}

busy = set()
last_run = {}

HELP = (
    "Ищу свободные юзернеймы из 5 или 6 букв. Выбери кнопкой или командой:\n\n"
    "/find [5|6] [N] - красивые (читаемые)\n"
    "/random [5|6] [N] - полностью случайные\n"
    "/check имя - проверить конкретный юз\n\n"
    "Пример: /find 6 30 - 30 красивых по 6 букв.\n"
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


def send(chat_id, text, markup=None):
    params = {"chat_id": chat_id, "text": text}
    if markup:
        params["reply_markup"] = markup
    try:
        api("sendMessage", **params)
    except Exception as e:
        print("send error:", e, flush=True)


def gen_name(kind, length):
    if kind == "rand":
        return "".join(random.choice(string.ascii_lowercase) for _ in range(length))
    p = random.choice(PATTERNS[length])
    return "".join(random.choice(CONS if c == "C" else VOWELS) for c in p)


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


def search(chat_id, kind, length, n):
    try:
        send(chat_id, "Проверяю %d юзов по %d букв, подожди ~%d сек..." % (n, length, int(n * DELAY)))
        found, seen, errors, done = [], set(), 0, 0
        while done < n and errors < 5:
            name = gen_name(kind, length)
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
            text = "Похоже свободны (%d из %d):\n%s" % (len(found), done, "\n".join(found))
        else:
            text = "Проверено %d, свободных не нашлось. Попробуй ещё раз." % done
        if errors >= 5:
            text += "\n\nt.me часто не отвечает, остановился. Попробуй позже."
        send(chat_id, text, KEYBOARD)
    finally:
        busy.discard(chat_id)


def start_search(chat_id, kind, length, n):
    if chat_id in busy:
        send(chat_id, "Поиск уже идёт, дождись результата.")
        return
    wait = COOLDOWN - (time.time() - last_run.get(chat_id, 0))
    if wait > 0:
        send(chat_id, "Подожди ещё %d сек." % int(wait))
        return
    last_run[chat_id] = time.time()
    busy.add(chat_id)
    threading.Thread(target=search, args=(chat_id, kind, length, n), daemon=True).start()


def parse_args(parts):
    """/find [5|6] [N] -> (длина, N)"""
    args = parts[1:]
    length = 5
    n = DEFAULT_N
    if args and args[0] in ("5", "6"):
        length = int(args[0])
        args = args[1:]
    if args and args[0].isdigit():
        n = max(1, min(int(args[0]), MAX_N))
    return length, n


def handle(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()

    if cmd in ("/start", "/help"):
        send(chat_id, HELP, KEYBOARD)
        return

    if cmd == "/check":
        if len(parts) < 2 or not re.fullmatch(r"[A-Za-z]{5,6}", parts[1].lstrip("@")):
            send(chat_id, "Напиши так: /check abcde (5 или 6 букв)")
            return
        name = parts[1].lstrip("@").lower()
        res = check(name)
        if res is None:
            send(chat_id, "Не удалось проверить, попробуй позже.")
        elif res:
            send(chat_id, "@%s - похоже свободен" % name)
        else:
            send(chat_id, "@%s - занят" % name)
        return

    if cmd in ("/find", "/random"):
        length, n = parse_args(parts)
        kind = "find" if cmd == "/find" else "rand"
        start_search(chat_id, kind, length, n)


def handle_callback(cb):
    try:
        api("answerCallbackQuery", callback_query_id=cb["id"])
    except Exception:
        pass
    data = cb.get("data", "")
    msg = cb.get("message")
    if not msg or ":" not in data:
        return
    kind, length = data.split(":", 1)
    if kind in ("find", "rand") and length in ("5", "6"):
        start_search(msg["chat"]["id"], kind, int(length), DEFAULT_N)


def keepalive():
    """Нужен для хостингов, которые требуют открытый порт (например Render)."""
    port = os.environ.get("PORT")
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("0.0.0.0", int(port)), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def main():
    if not TOKEN:
        print("Нет токена")
        return
    keepalive()
    print("Бот запущен", flush=True)
    offset = 0
    while True:
        try:
            r = api("getUpdates", offset=offset, timeout=30)
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    handle(upd["message"])
                elif "callback_query" in upd:
                    handle_callback(upd["callback_query"])
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("poll error:", e, flush=True)
            time.sleep(5)


main()
