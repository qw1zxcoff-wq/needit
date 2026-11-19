import itertools
import json
import os
import random
import re
import string
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

TOKEN = os.environ.get("BOT_TOKEN") or "9159943768:AAF7uZuTYZ2QtIcYRSIjF44NDnpUb7pbOoQ"
API = "https://api.telegram.org/bot%s/" % TOKEN

# ================= НАСТРОЙКИ =================
# Цвета кнопок: "primary" (синий), "success" (зелёный), "danger" (красный), "" (без цвета)
STYLE_FIND = "primary"
STYLE_RAND = "success"
STYLE_FAV = ""
STYLE_WATCH = ""
STYLE_CLEAR = "danger"
STYLE_LEN_ON = "primary"   # выбранная длина
STYLE_LEN_OFF = ""

# ID премиум-эмодзи для кнопок (оставь "" если не нужно)
ICON_FIND = ""
ICON_RAND = ""

# Доступ: в Render -> Environment добавь ALLOWED_IDS=твойID (несколько через запятую).
# Пока переменной нет, ботом может пользоваться любой. Свой ID покажет /myid
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x.isdigit()}

DELAY = 1.2            # пауза каждого потока между проверками (сек)
WORKERS = 4            # потоков проверки
DEFAULT_N = 20
MAX_N = 60
COOLDOWN = 15
MIN_LEN = 5
MAX_LEN = 12
WATCH_INTERVAL = 300   # как часто проверять слежки (сек)
MAX_WATCH = 15
AUTO_INTERVAL = 3600   # автопоиск раз в час
AUTO_N = 30
PING_INTERVAL = 15    # самопинг, чтобы Render не усыплял (сек), можно хоть 5
MAX_TAKEN = 20000      # сколько занятых юзов помнить
STATE_FILE = "state.json"
DEFAULT_URL = "https://needit-li4n.onrender.com"
# =============================================

VOWELS = "aeiou"
CONS = "bcdfghjklmnpqrstvwxz"
USER_RE = re.compile(r"^[a-z][a-z0-9_]{4,31}$")

WORDS = """
alpha brave blaze cloud crown dream drift eagle ember flame frost ghost giant glory grace haven
honey ivory jewel karma lunar magic maple noble ocean olive orbit panda pearl pilot pixel prime
pulse quest raven rebel river royal shade shark shine skull sleep smoke solar spark spice storm
swift tiger titan toxic ultra unity venom vivid wraith zenith zephyr shadow silent thunder
phoenix falcon hunter legend mirage nebula oracle spirit stealth viking wizard cobalt comet
cosmos crystal dragon galaxy hermit jungle knight lotus meteor nomad plasma quartz rocket
sierra tundra vortex walrus yonder zodiac
""".split()
WORDS = [w for w in WORDS if w.isalpha() and 5 <= len(w) <= 12]

lock = threading.RLock()
state = {
    "watches": {},     # юз -> [chat_id, ...]
    "favs": {},        # chat_id -> [юзы]
    "settings": {},    # chat_id -> настройки
    "stats": {"checked": 0, "found": 0},
    "taken": [],
    "sent": {},        # chat_id -> уже присланные автопоиском
}
taken = set()
busy = set()
last_run = {}
STARTED = time.time()

HELP = (
    "Ищу свободные юзернеймы длиной 5-12 букв. Выбирай кнопками или командами:\n\n"
    "/find [N] - красивые (читаемые)\n"
    "/random [N] - случайные\n"
    "/mask a??ie - по маске (? = любая буква)\n"
    "/words [слова] - из списка слов или из твоих\n"
    "/check имя - проверить один юз\n"
    "/len 8 - длина юза (5-12)\n"
    "/norep on|off - без повторяющихся букв\n"
    "/charset letters|alnum - буквы или буквы+цифры (для случайных)\n"
    "/watch имя - следить, напишу когда освободится\n"
    "/unwatch имя, /watchlist\n"
    "/fav, /fav имя, /unfav имя - избранное\n"
    "/auto on|off - автопоиск раз в час, присылаю только новые\n"
    "/stats - статистика, /myid - твой ID\n\n"
    "Пример: /find 8 30 - 30 красивых по 8 букв.\n"
    "Пометка «свободен?» не гарантия: юз может быть зарезервирован или на Fragment.\n"
    "Отправь мне премиум-эмодзи, и я пришлю его ID."
)


# ---------- сохранение ----------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        with lock:
            for k in state:
                if k in data:
                    state[k] = data[k]
            taken.update(state.get("taken", []))
    except Exception:
        pass


def save_state():
    try:
        with lock:
            lst = list(taken)
            if len(lst) > MAX_TAKEN:
                lst = lst[-MAX_TAKEN:]
                taken.clear()
                taken.update(lst)
            state["taken"] = lst
            raw = json.dumps(state, ensure_ascii=False)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(raw)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print("save error:", e, flush=True)


def cfg(chat_id):
    with lock:
        s = state["settings"].setdefault(str(chat_id), {})
        s.setdefault("length", 5)
        s.setdefault("norep", False)
        s.setdefault("charset", "letters")
        s.setdefault("auto", False)
        return s


def allowed(chat_id):
    return not ALLOWED or chat_id in ALLOWED


# ---------- Telegram API ----------

def api(method, **params):
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        API + method, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


def btn(text, data, style="", icon="", emoji="", plain=False):
    label = text if (icon or not emoji) else emoji + " " + text
    b = {"text": label, "callback_data": data}
    if not plain:
        if style:
            b["style"] = style
        if icon:
            b["icon_custom_emoji_id"] = icon
    return b


def make_keyboard(chat_id, plain=False, fav_names=None):
    s = cfg(chat_id)
    L = s["length"]
    rows = []
    names = fav_names or []
    for i in range(0, len(names), 2):
        rows.append([btn("@" + n, "fav:" + n, STYLE_FAV, "", "⭐", plain) for n in names[i:i + 2]])
    rows.append([
        btn("красивые · %d" % L, "go:find", STYLE_FIND, ICON_FIND, "✨", plain),
        btn("случайные · %d" % L, "go:rand", STYLE_RAND, ICON_RAND, "🎲", plain),
    ])
    for start in (MIN_LEN, MIN_LEN + 4):
        rows.append([
            btn(("• %d" if n == L else "%d") % n, "len:%d" % n,
                STYLE_LEN_ON if n == L else STYLE_LEN_OFF, "", "", plain)
            for n in range(start, min(start + 4, MAX_LEN + 1))
        ])
    rows.append([
        btn("Слежки", "list", STYLE_WATCH, "", "👁", plain),
        btn("Избранное", "favs", STYLE_FAV, "", "⭐", plain),
    ])
    rows.append([btn("Очистить слежки", "clear", STYLE_CLEAR, "", "🗑", plain)])
    return {"inline_keyboard": rows}


def send(chat_id, text, keyboard=False, fav_names=None):
    params = {"chat_id": chat_id, "text": text[:3900]}
    try:
        if keyboard:
            try:
                api("sendMessage", reply_markup=make_keyboard(chat_id, False, fav_names), **params)
            except Exception as e:
                print("keyboard error, fallback:", e, flush=True)
                api("sendMessage", reply_markup=make_keyboard(chat_id, True, fav_names), **params)
        else:
            api("sendMessage", **params)
    except Exception as e:
        print("send error:", e, flush=True)


def answer(cb_id, text=None):
    try:
        if text:
            api("answerCallbackQuery", callback_query_id=cb_id, text=text)
        else:
            api("answerCallbackQuery", callback_query_id=cb_id)
    except Exception:
        pass


def edit_keyboard(chat_id, message_id):
    for plain in (False, True):
        try:
            api("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                reply_markup=make_keyboard(chat_id, plain))
            return
        except Exception:
            continue


# ---------- генерация и проверка ----------

def valid(name):
    return bool(USER_RE.match(name)) and not name.endswith("_")


def gen_pretty(length, norep=False):
    types = [random.choice("CV")]
    while len(types) < length:
        if len(types) >= 2 and types[-1] == types[-2]:
            types.append("V" if types[-1] == "C" else "C")
        elif random.random() < 0.72:
            types.append("V" if types[-1] == "C" else "C")
        else:
            types.append(types[-1])
    cons, vows = list(CONS), list(VOWELS)
    out = []
    for c in types:
        if norep:
            pool = cons if (c == "C" and cons) or not vows else vows
            ch = random.choice(pool)
            pool.remove(ch)
        else:
            ch = random.choice(CONS if c == "C" else VOWELS)
        out.append(ch)
    return "".join(out)


def gen_random(length, charset, norep=False):
    pool = string.ascii_lowercase + (string.digits if charset == "alnum" else "")
    first = random.choice(string.ascii_lowercase)
    if norep:
        rest = random.sample([c for c in pool if c != first], length - 1)
    else:
        rest = [random.choice(pool) for _ in range(length - 1)]
    return first + "".join(rest)


def make_name(kind, length, norep, charset):
    if kind == "find":
        return gen_pretty(length, norep)
    return gen_random(length, charset, norep)


def fill_mask(mask):
    return "".join(random.choice(string.ascii_lowercase) if c == "?" else c for c in mask)


def mask_namer(mask):
    k = mask.count("?")
    if k <= 2:
        combos = ["".join(c) for c in itertools.product(string.ascii_lowercase, repeat=k)]
        random.shuffle(combos)
        it = iter(combos)

        def nxt():
            c = next(it, None)
            if c is None:
                return None
            letters = iter(c)
            return "".join(next(letters) if ch == "?" else ch for ch in mask)

        return nxt
    return lambda: fill_mask(mask)


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


def record(name, res, cached):
    with lock:
        state["stats"]["checked"] += 1
        if res:
            state["stats"]["found"] += 1
        elif not cached:
            taken.add(name)


def scan(next_name, n):
    """Проверяет до n юзов в несколько потоков. Возвращает (найдено, проверено, упёрлись_в_лимит)."""
    found = []
    ctl = {"picked": 0, "done": 0, "errors": 0, "attempts": 0}
    seen = set()
    glock = threading.Lock()
    max_attempts = n * 30 + 100

    def worker():
        while True:
            with glock:
                if ctl["picked"] >= n or ctl["errors"] >= 6 or ctl["attempts"] >= max_attempts:
                    return
                ctl["attempts"] += 1
                name = next_name()
                if name is None:
                    return
                if name in seen or not valid(name):
                    continue
                seen.add(name)
                ctl["picked"] += 1
            with lock:
                cached = name in taken
            if cached:
                res = False
            else:
                res = check(name)
                time.sleep(DELAY)
            with glock:
                if res is None:
                    ctl["picked"] -= 1
                    ctl["errors"] += 1
                    continue
                ctl["errors"] = 0
                ctl["done"] += 1
                if res:
                    found.append(name)
            record(name, res, cached)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for _ in range(WORKERS):
            ex.submit(worker)
    return found, ctl["done"], ctl["errors"] >= 6


def search(chat_id, title, next_name, n):
    try:
        send(chat_id, "%s: проверяю %d юзов, подожди ~%d сек..." % (title, n, int(n * DELAY / WORKERS) + 3))
        found, done, limited = scan(next_name, n)
        if found:
            text = "Похоже свободны (%d из %d):\n%s\n\nНажми на юз ниже, чтобы добавить в избранное." % (
                len(found), done, "\n".join("@" + x for x in found))
        else:
            text = "Проверено %d, свободных не нашлось. Попробуй ещё раз." % done
        if limited:
            text += "\n\nt.me часто не отвечает, остановился. Попробуй позже."
        save_state()
        send(chat_id, text, keyboard=True, fav_names=found[:8])
    finally:
        busy.discard(chat_id)


def start_search(chat_id, title, next_name, n):
    if chat_id in busy:
        send(chat_id, "Поиск уже идёт, дождись результата.")
        return
    wait = COOLDOWN - (time.time() - last_run.get(chat_id, 0))
    if wait > 0:
        send(chat_id, "Подожди ещё %d сек." % int(wait))
        return
    last_run[chat_id] = time.time()
    busy.add(chat_id)
    threading.Thread(target=search, args=(chat_id, title, next_name, n), daemon=True).start()


def launch(chat_id, kind, length=None, n=DEFAULT_N):
    s = cfg(chat_id)
    L = length or s["length"]
    title = ("Красивые" if kind == "find" else "Случайные") + " по %d" % L
    start_search(chat_id, title, lambda: make_name(kind, L, s["norep"], s["charset"]), n)


def parse_num_args(parts):
    """/find [длина] [N] -> (длина или None, N)"""
    nums = [int(x) for x in parts[1:3] if x.isdigit()]
    length, n = None, DEFAULT_N
    if len(nums) >= 2 and MIN_LEN <= nums[0] <= MAX_LEN:
        length, n = nums[0], nums[1]
    elif len(nums) == 1:
        n = nums[0]
    return length, max(1, min(n, MAX_N))


# ---------- слежка и избранное ----------

def my_watches(chat_id):
    with lock:
        return sorted(n for n, cs in state["watches"].items() if chat_id in cs)


def add_watch(chat_id, name):
    with lock:
        cs = state["watches"].setdefault(name, [])
        if chat_id not in cs:
            cs.append(chat_id)
    save_state()


def remove_watch(chat_id, name):
    with lock:
        cs = state["watches"].get(name)
        if not cs or chat_id not in cs:
            return False
        cs.remove(chat_id)
        if not cs:
            del state["watches"][name]
    save_state()
    return True


def clear_watches(chat_id):
    for n in my_watches(chat_id):
        remove_watch(chat_id, n)


def send_watchlist(chat_id):
    names = my_watches(chat_id)
    if names:
        send(chat_id, "Слежу за:\n" + "\n".join("@" + n for n in names), keyboard=True)
    else:
        send(chat_id, "Слежек нет. Добавь: /watch abcde", keyboard=True)


def get_favs(chat_id):
    with lock:
        return list(state["favs"].get(str(chat_id), []))


def add_fav(chat_id, name):
    with lock:
        lst = state["favs"].setdefault(str(chat_id), [])
        if name in lst:
            return False
        lst.append(name)
    save_state()
    return True


def remove_fav(chat_id, name):
    with lock:
        lst = state["favs"].get(str(chat_id), [])
        if name not in lst:
            return False
        lst.remove(name)
    save_state()
    return True


def send_favs(chat_id):
    names = get_favs(chat_id)
    if names:
        send(chat_id, "Избранное:\n" + "\n".join("@" + n for n in names) +
             "\n\nСледить: /watch имя. Убрать: /unfav имя.", keyboard=True)
    else:
        send(chat_id, "Избранное пусто. Нажми ⭐ под найденным юзом.", keyboard=True)


def cmd_watch(chat_id, parts):
    if len(parts) < 2:
        send(chat_id, "Напиши так: /watch abcde")
        return
    name = parts[1].lstrip("@").lower()
    if not valid(name):
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
        add_watch(chat_id, name)
        send(chat_id, "Слежу за @%s. Проверяю раз в %d мин и напишу, когда освободится." % (name, WATCH_INTERVAL // 60))


def watcher():
    while True:
        with lock:
            names = list(state["watches"].keys())
        for name in names:
            res = check(name)
            time.sleep(DELAY)
            if res:
                time.sleep(10)
                if check(name) is True:  # перепроверка против ложной тревоги
                    with lock:
                        chats = list(state["watches"].pop(name, []))
                    save_state()
                    for c in chats:
                        send(c, "🔔 @%s похоже освободился! Проверь и займи как можно быстрее." % name)
        time.sleep(WATCH_INTERVAL)


def auto_worker():
    while True:
        time.sleep(AUTO_INTERVAL)
        with lock:
            chats = [int(c) for c, s in state["settings"].items() if s.get("auto")]
        for chat_id in chats:
            try:
                s = cfg(chat_id)
                found, done, limited = scan(
                    lambda: make_name("find", s["length"], s["norep"], s["charset"]), AUTO_N)
                with lock:
                    sent = state["sent"].setdefault(str(chat_id), [])
                    new = [x for x in found if x not in sent]
                    sent.extend(new)
                    del sent[:-500]
                save_state()
                if new:
                    send(chat_id, "🔔 Автопоиск, новые похоже свободные (%d):\n%s" % (
                        len(new), "\n".join("@" + x for x in new)), keyboard=True, fav_names=new[:8])
            except Exception as e:
                print("auto error:", e, flush=True)


def pinger():
    """Самопинг своей ссылки: только входящий HTTP-запрос не даёт Render усыпить сервис."""
    url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("SELF_URL")
    if not url and os.environ.get("PORT"):
        url = DEFAULT_URL
    if not url:
        return
    while True:
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                r.read()
                print("ping ok", r.status, time.strftime("%H:%M:%S"), flush=True)
        except Exception as e:
            print("ping error:", e, flush=True)
        time.sleep(PING_INTERVAL)


# ---------- обработка сообщений ----------

def fmt_uptime():
    sec = int(time.time() - STARTED)
    return "%dч %dм" % (sec // 3600, (sec % 3600) // 60)


def send_stats(chat_id):
    s = cfg(chat_id)
    with lock:
        st = dict(state["stats"])
        tk = len(taken)
        total_w = len(state["watches"])
    send(chat_id,
         "Статистика:\nПроверено: %d\nНайдено свободных: %d\nЗанятых в памяти: %d\n"
         "Слежек (твоих/всех): %d/%d\nИзбранное: %d\nДлина: %d, без повторов: %s, символы: %s, автопоиск: %s\n"
         "Работаю: %s" % (
             st["checked"], st["found"], tk, len(my_watches(chat_id)), total_w,
             len(get_favs(chat_id)), s["length"], "да" if s["norep"] else "нет",
             s["charset"], "вкл" if s["auto"] else "выкл", fmt_uptime()),
         keyboard=True)


def handle(msg):
    chat_id = msg["chat"]["id"]

    # если прислали премиум-эмодзи, показываем его ID
    ids = [
        e["custom_emoji_id"]
        for e in (msg.get("entities") or [])
        if e.get("type") == "custom_emoji" and e.get("custom_emoji_id")
    ]
    if ids and allowed(chat_id):
        send(chat_id, "ID премиум-эмодзи:\n" + "\n".join(ids))
        return

    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()

    if cmd == "/myid":
        send(chat_id, "Твой ID: %d" % chat_id)
        return
    if not allowed(chat_id):
        send(chat_id, "Нет доступа. Твой ID: %d" % chat_id)
        return

    s = cfg(chat_id)

    if cmd in ("/start", "/help"):
        send(chat_id, HELP, keyboard=True
