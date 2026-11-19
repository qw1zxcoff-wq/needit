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

# ЦВЕТА КНОПОК: "primary" (синий), "success" (зелёный), "danger" (красный), "" (без цвета)
STYLE_FIND = "primary"
STYLE_RAND = "success"
STYLE_LIST = ""
STYLE_CLEAR = "danger"

# ID премиум-эмодзи для кнопок (оставь "" если не нужно)
ICON_FIND = ""
ICON_RAND = ""

DELAY = 1.5
DEFAULT_N = 20
MAX_N = 40
COOLDOWN = 20
WATCH_INTERVAL = 300   # как часто проверять слежки (сек)
MAX_WATCH = 10         # максимум слежек на человека

VOWELS = "aeiou"
CONS = "bcdfghjklmnpqrstvwxz"
PATTERNS = {
    5: ["CVCVC", "CVCCV", "CCVCV", "VCVCV", "CVVCV", "VCCVC"],
    6: ["CVCVCV", "CVCCVC", "CCVCVC", "VCVCVC", "CVVCVC", "VCCVCV"],
}

busy = set()
last_run = {}
watches = {}  # юз -> множество chat_id
lock = threading.Lock()

HELP = (
    "Ищу свободные юзернеймы из 5 или 6 букв. Выбери кнопкой или командой:\n\n"
    "/find [5|6] [N] - красивые (читаемые)\n"
    "/random [5|6] [N] - полностью случайные\n"
    "/check имя - проверить конкретный юз\n"
    "/watch имя - следить и написать, когда освободится\n"
    "/unwatch имя - перестать следить\n"
    "/watchlist - список слежек\n\n"
    "Пример: /find 6 30 - 30 красивых по 6 букв.\n"
    "N - сколько проверить (до %d, по умолчанию %d).\n"
    "Пометка «свободен?» не гарантия: юз может быть зарезервирован или на Fragment.\n\n"
    "Отправь мне премиум-эмодзи, и я пришлю его ID."
    % (MAX_N, DEFAULT_N)
)


def make_keyboard(plain=False):
    def btn(text, data, style, icon="", emoji=""):
        label = text if (icon or not emoji) else emoji + " " + text
        b = {"text": label, "callback_data": data}
        if not plain:
            if style:
                b["style"] = style
            if icon:
                b["icon_custom_emoji_id"] = icon
        return b

    return {
        "inline_keyboard": [
            [
                btn("5 букв · красивые", "find:5", STYLE_FIND, ICON_FIND, "✨"),
                btn("6 букв · красивые", "find:6", STYLE_FIND, ICON_FIND, "✨"),
            ],
            [
                btn("5 букв · случайные", "rand:5", STYLE_RAND, ICON_RAND, "🎲"),
                btn("6 букв · случайные", "rand:6", STYLE_RAND, ICON_RAND, "🎲"),
            ],
            [
                btn("Мои слежки", "list", STYLE_LIST, "", "👁"),
                btn("Очистить слежки", "clear", STYLE_CLEAR, "", "🗑"),
            ],
        ]
    }


def api(method, **params):
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        API + method, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


def send(chat_id, text, keyboard=False):
    params = {"chat_id": chat_id, "text": text}
    try:
        if keyboard:
            try:
                api("sendMessage", reply_markup=make_keyboard(), **params)
            except Exception as e:
                print("keyboard error, fallback:", e, flush=True)
                api("sendMessage", reply_markup=make_keyboard(plain=True), **params)
        else:
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
        send(chat_id, text, keyboard=True)
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


# ---------- слежка ----------

def my_watches(chat_id):
    with lock:
        return sorted(n for n, s in watches.items() if chat_id in s)


def send_watchlist(chat_id):
    names = my_watches(chat_id)
    if names:
        send(chat_id, "Слежу за:\n" + "\n".join("@" + n for n in names), keyboard=True)
    else:
        send(chat_id, "Слежек нет. Добавь: /watch abcde", keyboard=True)


def clear_watches(chat_id):
    with lock:
        for n in list(watches):
            watches[n].discard(chat_id)
            if not watches[n]:
                del watches[n]
    send(chat_id, "Все слежки удалены.", keyboard=True)


def cmd_watch(chat_id, parts):
    if len(parts) < 2:
        send(chat_id, "Напиши так: /watch abcde")
        return
    name = parts[1].lstrip("@").lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{4,31}", name):
        send(chat_id, "Юзернейм: 5-32 символа, латиница, цифры или _, начинается с буквы.")
        return
    if len(my_watches(chat_id)) >= MAX_WATCH:
        send(chat_id, "Максимум %d слежек. Убери лишние через /unwatch." % MAX_WATCH)
        return
    res = check(name)
    if res is None:
        send(chat_id, "Не удалось проверить, попробуй позже.")
    elif res:
        send(chat_id, "@%s уже похоже свободен, слежка не нужна." % name)
    else:
        with lock:
            watches.setdefault(name, set()).add(chat_id)
        send(chat_id, "Слежу за @%s. Проверяю раз в %d мин и напишу, когда освободится." % (name, WATCH_INTERVAL // 60))


def cmd_unwatch(chat_id, parts):
    if len(parts) < 2:
        send(chat_id, "Напиши так: /unwatch abcde")
        return
    name = parts[1].lstrip("@").lower()
    with lock:
        if name in watches and chat_id in watches[name]:
            watches[name].discard(chat_id)
            if not watches[name]:
                del watches[name]
            ok = True
        else:
            ok = False
    send(chat_id, "Больше не слежу за @%s." % name if ok else "За @%s ты не следишь." % name)


def watcher():
    while True:
        with lock:
            names = list(watches.keys())
        for name in names:
            res = check(name)
            time.sleep(DELAY)
            if res:
                time.sleep(10)
                if check(name) is True:  # перепроверка против ложной тревоги
                    with lock:
                        chats = list(watches.pop(name, set()))
                    for c in chats:
                        send(c, "🔔 @%s похоже освободился! Проверь и займи как можно быстрее." % name)
        time.sleep(WATCH_INTERVAL)


# ---------- обработка сообщений ----------

def handle(msg):
    chat_id = msg["chat"]["id"]

    # если прислали премиум-эмодзи, показываем его ID
    ids = [
        e["custom_emoji_id"]
        for e in (msg.get("entities") or [])
        if e.get("type") == "custom_emoji" and e.get("custom_emoji_id")
    ]
    if ids:
        send(chat_id, "ID премиум-эмодзи:\n" + "\n".join(ids))
        return

    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()

    if cmd in ("/start", "/help"):
        send(chat_id, HELP, keyboard=True)
    elif cmd == "/check":
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
    elif cmd in ("/find", "/random"):
        length, n = parse_args(parts)
        kind = "find" if cmd == "/find" else "rand"
        start_search(chat_id, kind, length, n)
    elif cmd == "/watch":
        cmd_watch(chat_id, parts)
    elif cmd == "/unwatch":
        cmd_unwatch(chat_id, parts)
    elif cmd == "/watchlist":
        send_watchlist(chat_id)


def handle_callback(cb):
    try:
        api("answerCallbackQuery", callback_query_id=cb["id"])
    except Exception:
        pass
    data = cb.get("data", "")
    msg = cb.get("message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    if data == "list":
        send_watchlist(chat_id)
    elif data == "clear":
        clear_watches(chat_id)
    elif ":" in data:
        kind, length = data.split(":", 1)
        if kind in ("find", "rand") and length in ("5", "6"):
            start_search(chat_id, kind, int(length), DEFAULT_N)


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
    threading.Thread(target=watcher, daemon=True).start()
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
