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
from collections import deque
from concurrent.futures import ThreadPoolExecutor

TOKEN = os.environ.get("BOT_TOKEN", "")  # токен только в Render -> Environment, в код его не вставляй
API = "https://api.telegram.org/bot%s/" % TOKEN

# ================= НАСТРОЙКИ =================
# Админ (только ему доступна админ-панель и команды). Можно добавить ещё через
# переменную ADMIN_IDS в Render (через запятую).
ADMIN_IDS = {6870042918} | {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

# Цвета кнопок: "primary" (синий), "success" (зелёный), "danger" (красный), "" (без цвета)
STYLE_FIND = "primary"
STYLE_RAND = "success"
STYLE_FAV = ""
STYLE_CLEAR = "danger"
STYLE_LEN_ON = "primary"   # выбранная длина
STYLE_LEN_OFF = ""

# ID премиум-эмодзи для кнопок (оставь "" если не нужно)
ICON_FIND = ""
ICON_RAND = ""

# Если задать ALLOWED_IDS в Render (через запятую), пользоваться ботом смогут только они и админ.
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x.isdigit()}

DELAY = 1.2            # минимальная пауза каждого потока между проверками (сек)
DELAY_MAX = 12.0       # максимальная пауза при лимитах t.me (адаптивная скорость)
BACKOFF_START = 15     # первая общая пауза после лимита 429 (сек), дальше удваивается
BACKOFF_MAX = 180      # максимум общей паузы (сек)
SPEEDUP_AFTER = 20     # после стольких удачных проверок подряд бот чуть ускоряется
LIMIT_STOP = 12        # сколько ответов-лимитов за один поиск терпим, потом останавливаем поиск
FRAGMENT_CHECK = True  # проверять найденные юзы на Fragment (продаются ли они там)
FRAG_MAX = 25          # сколько найденных юзов проверять на Fragment за один поиск
FRAG_DELAY = 0.8       # пауза между запросами к Fragment (сек)
WORKERS = 4            # потоков проверки
DEFAULT_N = 20
MAX_N = 60
COOLDOWN = 15
MIN_LEN = 5
MAX_LEN = 12
PROGRESS_EVERY = 2.0   # как часто обновлять сообщение с прогрессом (сек)
WATCH_INTERVAL = 300   # как часто проверять слежки (сек)
MAX_WATCH = 15
AUTO_INTERVAL = 3600   # автопоиск раз в час
AUTO_N = 30
PING_INTERVAL = 35     # самопинг, чтобы Render не усыплял (сек)
CONFLICT_COUNT = 4      # сколько ошибок 409 за окно считать «токен используют ещё где-то»
CONFLICT_WINDOW = 600   # окно (сек)
CONFLICT_COOLDOWN = 1800  # не чаще одного предупреждения за это время (сек)
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
    "users": {},       # chat_id -> {"first", "last", "n"}
    "banned": [],
    "private": False,  # закрытый режим: только админ и ALLOWED
    "maintenance": False,  # режим обслуживания: всем, кроме админа, «технические работы»
    "maint_text": "",
}
taken = set()
busy = set()
last_run = {}
last_job = {}      # chat_id -> (title, фабрика, n) для кнопки «Ещё»
last_found = {}    # chat_id -> последние найденные юзы
rare_shown = set()  # редкие юзы, которые уже показывали (чтобы не повторять)
errlog = deque(maxlen=30)
slock = threading.Lock()
speed = {"delay": DELAY, "until": 0.0, "backoff": BACKOFF_START, "ok": 0, "limits": 0, "last_limit": 0.0}
conflicts = deque(maxlen=100)
last_conflict_alert = [0.0]
STARTED = time.time()

HELP = (
    "Ищу свободные юзернеймы длиной 5-12 букв. Жми кнопки или пиши команды:\n\n"
    "/find [N] - красивые (читаемые)\n"
    "/random [N] - случайные\n"
    "/mask a??ie - по маске (? = любая буква)\n"
    "/words [слова] - из списка слов или из твоих\n"
    "/word слово [N] - варианты из одного слова (king: theking, kingx, k1ng...)\n"
    "/fragment имя - есть ли юз на Fragment (продаётся, аукцион, продан)\n"
    "/check имя - проверить один юз\n"
    "/len 8 - длина юза (5-12)\n"
    "/norep on|off - без повторяющихся букв\n"
    "/charset letters|alnum - буквы или буквы+цифры\n"
    "/watch имя - следить, напишу когда освободится\n"
    "/unwatch имя, /watchlist\n"
    "/fav, /fav имя, /unfav имя - избранное\n"
    "/auto on|off - автопоиск раз в час\n"
    "/menu - кнопки, /stats - статистика, /myid - твой ID\n\n"
    "Пример: /find 8 30 - 30 красивых по 8 букв.\n"
    "Пометка «свободен?» не гарантия: юз может быть зарезервирован или на Fragment."
)

ADMIN_HELP = (
    "Команды админа:\n"
    "/admin - панель с кнопками\n"
    "/broadcast текст - рассылка всем пользователям\n"
    "/ban ID, /unban ID - блокировка\n"
    "/users - список пользователей\n"
    "/maintenance on|off [текст] - режим обслуживания\n"
    "/rare [N] - поиск редких юзов (5 букв, без цифр и _, читаемые)"
)


MAINT_DEFAULT = "🛠 Технические работы. Бот скоро вернётся, попробуй чуть позже."


def logerr(text):
    print(text, flush=True)
    errlog.append("%s %s" % (time.strftime("%H:%M:%S"), text[:200]))


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
        logerr("save error: %s" % e)


def saver():
    while True:
        time.sleep(120)
        save_state()


def cfg(chat_id):
    with lock:
        s = state["settings"].setdefault(str(chat_id), {})
        s.setdefault("length", 5)
        s.setdefault("norep", False)
        s.setdefault("charset", "letters")
        s.setdefault("auto", False)
        return s


def is_admin(chat_id):
    return chat_id in ADMIN_IDS


def allowed(chat_id):
    if is_admin(chat_id):
        return True
    with lock:
        if chat_id in state["banned"]:
            return False
        if state["private"] and chat_id not in ALLOWED:
            return False
    if ALLOWED and chat_id not in ALLOWED:
        return False
    return True


def maintenance_on():
    with lock:
        return state["maintenance"]


def maint_message():
    with lock:
        return state.get("maint_text") or MAINT_DEFAULT


def set_maintenance(on, text=""):
    with lock:
        state["maintenance"] = bool(on)
        if on:
            state["maint_text"] = text.strip()
    save_state()


def touch(chat_id):
    with lock:
        u = state["users"].setdefault(str(chat_id), {"first": time.time(), "last": 0, "n": 0})
        u["last"] = time.time()
        u["n"] += 1


def ago(ts):
    sec = int(time.time() - ts)
    if sec < 60:
        return "только что"
    if sec < 3600:
        return "%d мин назад" % (sec // 60)
    if sec < 86400:
        return "%d ч назад" % (sec // 3600)
    return "%d дн назад" % (sec // 86400)


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


def make_keyboard(chat_id, kind="main", plain=False):
    s = cfg(chat_id)
    L = s["length"]
    rows = []
    if kind == "main":
        rows.append([
            btn("красивые · %d" % L, "go:find", STYLE_FIND, ICON_FIND, "✨", plain),
            btn("случайные · %d" % L, "go:rand", STYLE_RAND, ICON_RAND, "🎲", plain),
        ])
        last = [btn("Меню", "menu", "", "", "⚙", plain)]
        if is_admin(chat_id):
            last.append(btn("Админ", "adm:open", "", "", "🛠", plain))
        rows.append(last)
    elif kind == "result":
        if last_found.get(chat_id):
            rows.append([btn("Все в избранное", "favall", STYLE_FAV, "", "⭐", plain)])
        rows.append([
            btn("Ещё", "again", STYLE_FIND, "", "🔄", plain),
            btn("Меню", "menu", "", "", "⚙", plain),
        ])
    elif kind == "menu":
        for start in (MIN_LEN, MIN_LEN + 4):
            rows.append([
                btn(("• %d" if n == L else "%d") % n, "len:%d" % n,
                    STYLE_LEN_ON if n == L else STYLE_LEN_OFF, "", "", plain)
                for n in range(start, min(start + 4, MAX_LEN + 1))
            ])
        rows.append([
            btn("Слежки", "list", "", "", "👁", plain),
            btn("Избранное", "favs", "", "", "⭐", plain),
        ])
        rows.append([
            btn("Очистить слежки", "clear", STYLE_CLEAR, "", "🗑", plain),
            btn("Назад", "back", "", "", "⬅", plain),
        ])
    elif kind == "admin":
        with lock:
            priv = state["private"]
            maint = state["maintenance"]
        rows.append([
            btn("Статистика", "adm:stats", "", "", "📊", plain),
            btn("Юзеры", "adm:users", "", "", "👥", plain),
        ])
        rows.append([btn("Редкие юзы (5 букв)", "adm:rare", STYLE_FIND, "", "💎", plain)])
        rows.append([btn("Закрытый доступ: %s" % ("вкл" if priv else "выкл"), "adm:private",
                         "success" if priv else "", "", "🔒" if priv else "🔓", plain)])
        rows.append([btn("Обслуживание: %s" % ("вкл" if maint else "выкл"), "adm:maint",
                         STYLE_CLEAR if maint else "", "", "🛠", plain)])
        rows.append([
            btn("Рассылка", "adm:bc", "", "", "📢", plain),
            btn("Бан", "adm:ban", "", "", "🚫", plain),
        ])
        rows.append([
            btn("Сброс кэша", "adm:clear", "", "", "🧹", plain),
            btn("Ошибки", "adm:errors", "", "", "📜", plain),
        ])
        rows.append([btn("Перезапуск", "adm:restart", STYLE_CLEAR, "", "♻", plain)])
    elif kind == "confirm":
        rows.append([
            btn("Да, перезапустить", "adm:restart_yes", STYLE_CLEAR, "", "✅", plain),
            btn("Отмена", "adm:open2", "", "", "❌", plain),
        ])
    return {"inline_keyboard": rows}


def send_msg(chat_id, text, kind=None):
    """Отправляет сообщение, возвращает message_id или None."""
    params = {"chat_id": chat_id, "text": text[:3900]}
    for plain in ((False, True) if kind else (False,)):
        try:
            if kind:
                r = api("sendMessage", reply_markup=make_keyboard(chat_id, kind, plain), **params)
            else:
                r = api("sendMessage", **params)
            return r["result"]["message_id"]
        except Exception as e:
            logerr("send error: %s" % e)
    return None


def send(chat_id, text, kind=None):
    return send_msg(chat_id, text, kind) is not None


def edit_text(chat_id, message_id, text, kind=None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text[:3900]}
    for plain in ((False, True) if kind else (False,)):
        try:
            if kind:
                api("editMessageText", reply_markup=make_keyboard(chat_id, kind, plain), **params)
            else:
                api("editMessageText", **params)
            return True
        except Exception as e:
            if "not modified" in str(e):
                return True
            logerr("edit error: %s" % e)
    return False


def edit_keyboard(chat_id, message_id, kind):
    for plain in (False, True):
        try:
            api("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                reply_markup=make_keyboard(chat_id, kind, plain))
            return
        except Exception:
            continue


def answer(cb_id, text=None):
    try:
        if text:
            api("answerCallbackQuery", callback_query_id=cb_id, text=text)
        else:
            api("answerCallbackQuery", callback_query_id=cb_id)
    except Exception:
        pass


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


def words_factory(names, shuffle):
    def f():
        lst = random.sample(names, len(names)) if shuffle else list(names)
        it = iter(lst)
        return lambda: next(it, None)
    return f


TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", "і": "i", "ї": "yi", "є": "ye", "ґ": "g",
}
PREFIXES = ["the", "my", "its", "real", "mr", "x", "i", "im", "get", "go", "just", "ur", "xx", "one"]
SUFFIXES = ["x", "z", "o", "s", "y", "ly", "hq", "pro", "tv", "xd", "oo", "gg", "io", "max", "er", "ix", "real", "ok"]
NUM_SUFFIXES = ["1", "7", "9", "01", "13", "21", "69", "99", "007", "123"]
LEET = {"a": "4", "e": "3", "i": "1", "o": "0", "s": "5"}


def translit(text):
    """Русские/украинские буквы -> латиница, лишнее выкидываем."""
    out = "".join(TRANSLIT.get(ch, ch) for ch in text.lower())
    return re.sub(r"[^a-z0-9]", "", out)


def word_variants(word):
    """Варианты юзернейма на основе слова. Без повторов, только допустимые. Само слово идёт первым."""
    seen, out = set(), []

    def add(x):
        if x not in seen and valid(x):
            seen.add(x)
            out.append(x)

    w = word
    add(w)
    for p in PREFIXES:
        add(p + w)
    for x in SUFFIXES:
        add(w + x)
    for d in NUM_SUFFIXES:
        add(w + d)
    for x in ("x", "o", "z", "hq"):
        add(w + "_" + x)
    for p in ("x", "the", "my"):
        add(p + "_" + w)
    add(w + w[-1])
    add(w[0] + w)
    for v in "aeiouy":
        add(w + v)
    for i, ch in enumerate(w):
        if ch in VOWELS:
            for v in VOWELS:
                if v != ch:
                    add(w[:i] + v + w[i + 1:])
            if i > 0:
                add(w[:i] + w[i + 1:])
        if ch in LEET:
            add(w[:i] + LEET[ch] + w[i + 1:])
    add("".join(LEET.get(ch, ch) for ch in w))
    for i in range(len(w) - 1):
        if w[i] != w[i + 1]:
            add(w[:i] + w[i + 1] + w[i] + w[i + 2:])
    return out


def word_factory(word):
    def f():
        with lock:
            cand = [v for v in word_variants(word) if v not in taken]
        first = cand[:1] if cand and cand[0] == word else []
        rest = cand[len(first):]
        random.shuffle(rest)
        it = iter(first + rest)
        return lambda: next(it, None)
    return f


# ---------- редкие юзы (только для админа) ----------
# Критерии: ровно 5 символов, только буквы (без цифр и _), легко читаются, буквы не повторяются.
# «Редкость» это оценка бота по редким буквам, а не официальные данные Telegram.
RCONS = CONS.replace("q", "")
CLUSTERS = ["st", "nd", "rk", "rt", "lm", "lk", "sk", "mb", "nt", "rn", "lv", "rv", "ld", "mp"]
LETTER_W = {"e": 1, "t": 1, "a": 1, "o": 1, "i": 1, "n": 1, "s": 1, "r": 1, "h": 1, "l": 1, "d": 1,
            "c": 2, "u": 2, "m": 2, "w": 3, "f": 2, "g": 2, "y": 3, "p": 2, "b": 2,
            "v": 3, "k": 3, "j": 4, "x": 4, "z": 4}


def gen_rare_candidate():
    pattern = random.choice(("CVCVC", "VCVCV", "CVKV"))
    parts = []
    for ch in pattern:
        if ch == "C":
            parts.append(random.choice(RCONS))
        elif ch == "V":
            parts.append(random.choice(VOWELS))
        else:
            parts.append(random.choice(CLUSTERS))
    return "".join(parts)


def rare_score(name):
    sc = sum(LETTER_W.get(c, 1) for c in name)
    if name[0] in VOWELS:
        sc += 2
    return sc


def rare_stars(score):
    return 1 + sum(score >= t for t in (9, 11, 13, 15))


def rare_factory():
    def f():
        with lock:
            skip = set(taken)
        skip |= rare_shown
        pool, tries = set(), 0
        while len(pool) < 500 and tries < 20000:
            tries += 1
            nm = gen_rare_candidate()
            if len(set(nm)) == len(nm) and nm not in skip:
                pool.add(nm)
        ranked = sorted(pool, key=lambda x: rare_score(x) + random.random() * 2, reverse=True)
        it = iter(ranked)
        return lambda: next(it, None)
    return f


def launch_rare(chat_id, n=DEFAULT_N):
    start_search(chat_id, "💎 Редкие по 5 (читаемые, без цифр и _)", rare_factory(), n, rank=rare_score)


# ---------- адаптивная скорость ----------
# При лимите 429 от t.me все потоки ждут общую паузу (она растёт: 15, 30, 60... сек),
# а обычная задержка между проверками увеличивается. После серии удачных ответов бот снова ускоряется.

def current_delay():
    with slock:
        return speed["delay"] * random.uniform(0.9, 1.2)


def speed_wait():
    while True:
        with slock:
            wait = speed["until"] - time.time()
        if wait <= 0:
            return
        time.sleep(min(wait, 2))


def speed_limit():
    with slock:
        now = time.time()
        speed["limits"] += 1
        speed["last_limit"] = now
        speed["ok"] = 0
        if now >= speed["until"]:  # несколько потоков могли поймать один и тот же лимит
            speed["until"] = now + speed["backoff"]
            wait = speed["backoff"]
            speed["backoff"] = min(speed["backoff"] * 2, BACKOFF_MAX)
            speed["delay"] = min(speed["delay"] * 1.6, DELAY_MAX)
        else:
            wait = speed["until"] - now
        delay = speed["delay"]
    logerr("t.me: лимит 429, общая пауза %d сек, задержка теперь %.1f сек" % (wait, delay))


def speed_ok():
    with slock:
        speed["ok"] += 1
        if speed["ok"] >= SPEEDUP_AFTER:
            speed["ok"] = 0
            speed["delay"] = max(DELAY, speed["delay"] * 0.85)
            speed["backoff"] = max(BACKOFF_START, speed["backoff"] / 2)


def recently_limited(seconds=5):
    with slock:
        return time.time() - speed["last_limit"] < seconds


def speed_note():
    with slock:
        waiting = speed["until"] - time.time()
        delay = speed["delay"]
    if waiting > 0:
        return "\n🐢 t.me ограничил запросы, жду %d сек" % max(1, int(waiting))
    if delay > DELAY * 1.5:
        return "\n🐢 t.me притормаживает, иду медленнее (пауза %.1f сек)" % delay
    return ""


def check(name):
    """True = похоже свободен, False = занят, None = ошибка/лимит"""
    speed_wait()
    req = urllib.request.Request(
        "https://t.me/" + name,
        headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            speed_limit()
        return None
    except Exception:
        return None
    speed_ok()
    return "tgme_page_title" not in html


# ---------- Fragment ----------
# Юзы, выставленные на Fragment, t.me показывает как «свободные», хотя занять их обычным способом нельзя.
FRAG_STATUS_RE = re.compile(r'tm-section-header-status[^>]*>\s*([^<]+?)\s*<', re.I)


def fragment_status(name):
    """Возвращает (код, текст статуса).
    Коды: none (на Fragment нет), listed (продаётся/аукцион/доступен), sold, taken (занят, не продаётся),
    other (непонятный статус, текст показываем как есть), None (не удалось проверить)."""
    req = urllib.request.Request(
        "https://fragment.com/username/" + name,
        headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
                 "Accept-Language": "en-US,en;q=0.8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "none", ""
        return None, "HTTP %d" % e.code
    except Exception as e:
        return None, str(e)[:60]
    m = FRAG_STATUS_RE.search(html)
    if not m:
        return "none", ""
    raw = m.group(1).strip()
    low = raw.lower()
    if "sold" in low:
        return "sold", raw
    if "unavail" in low or "taken" in low or "claimed" in low:
        return "taken", raw
    if "auction" in low or "sale" in low or "avail" in low:
        return "listed", raw
    return "other", raw


def frag_text(name, code, raw):
    if code == "none":
        return "Fragment: нет в продаже"
    if code in ("listed", "other"):
        return "🛒 Fragment: %s (fragment.com/username/%s)" % (raw, name)
    if code in ("sold", "taken"):
        return "Fragment: %s (занят, купить нельзя)" % raw
    return "Fragment: не удалось проверить"


def mark_taken(name):
    with lock:
        taken.add(name)


def fragment_split(names):
    """Делит найденные юзы: (свободные, [(юз, статус)] на Fragment, непроверенные, сколько исключено)."""
    free, listed, unchecked, excluded = [], [], [], 0
    for i, x in enumerate(names):
        if i >= FRAG_MAX:
            unchecked.append(x)
            continue
        code, raw = fragment_status(x)
        time.sleep(FRAG_DELAY)
        if code == "none":
            free.append(x)
        elif code in ("listed", "other"):
            listed.append((x, raw))
            mark_taken(x)
        elif code in ("sold", "taken"):
            excluded += 1
            mark_taken(x)
        else:
            unchecked.append(x)
    return free, listed, unchecked, excluded


def record(name, res, cached):
    with lock:
        state["stats"]["checked"] += 1
        if res:
            state["stats"]["found"] += 1
        elif not cached:
            taken.add(name)


def scan(next_name, n, progress=None):
    """Проверяет до n юзов в несколько потоков. Возвращает (найдено, проверено, упёрлись_в_лимит)."""
    found = []
    ctl = {"picked": 0, "done": 0, "errors": 0, "limits": 0, "attempts": 0}
    seen = set()
    glock = threading.Lock()
    max_attempts = n * 30 + 100

    def worker():
        while True:
            with glock:
                if (ctl["picked"] >= n or ctl["errors"] >= 6 or ctl["limits"] >= LIMIT_STOP
                        or ctl["attempts"] >= max_attempts):
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
                time.sleep(current_delay())
            with glock:
                if res is None:
                    ctl["picked"] -= 1
                    if recently_limited():
                        ctl["limits"] += 1  # лимит t.me: бот сам подождёт и замедлится
                    else:
                        ctl["errors"] += 1
                    continue
                ctl["errors"] = 0
                ctl["done"] += 1
                if res:
                    found.append(name)
                snap = (ctl["done"], len(found))
            record(name, res, cached)
            if progress:
                try:
                    progress(*snap)
                except Exception:
                    pass

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for _ in range(WORKERS):
            ex.submit(worker)
    return found, ctl["done"], ctl["errors"] >= 6 or ctl["limits"] >= LIMIT_STOP


def progress_text(title, done, n, found_cnt):
    filled = int(10 * done / n) if n else 0
    return "%s\n%s %d/%d\nНайдено: %d%s" % (title, "▓" * filled + "░" * (10 - filled), done, n, found_cnt, speed_note())


def search(chat_id, title, make_next, n, rank=None):
    try:
        mid = send_msg(chat_id, progress_text(title, 0, n, 0))
        plock = threading.Lock()
        last_edit = [time.time()]

        def on_progress(done, found_cnt):
            if not mid:
                return
            with plock:
                now = time.time()
                if now - last_edit[0] < PROGRESS_EVERY:
                    return
                last_edit[0] = now
            edit_text(chat_id, mid, progress_text(title, done, n, found_cnt))

        found, done, limited = scan(make_next(), n, on_progress)
        if rank:
            found.sort(key=rank, reverse=True)
            rare_shown.update(found)
        free_names, listed, unchecked, excluded = list(found), [], [], 0
        if FRAGMENT_CHECK and found:
            if mid:
                edit_text(chat_id, mid, "%s\nПроверяю на Fragment: %d юз..." % (title, min(len(found), FRAG_MAX)))
            free_names, listed, unchecked, excluded = fragment_split(found)
        last_found[chat_id] = list(free_names) + list(unchecked)
        blocks = []
        if free_names:
            lines = [("@%s %s" % (x, "💎" * rare_stars(rank(x)))) if rank else "@" + x for x in free_names]
            blocks.append("✅ Свободны (%d):\n%s" % (len(free_names), "\n".join(lines)))
        if listed:
            blocks.append("🛒 На Fragment (%d):\n%s" % (
                len(listed), "\n".join("@%s - %s" % (x, raw) for x, raw in listed)))
        if unchecked:
            blocks.append("⚠️ Fragment не проверил (могут быть свободны):\n%s" % "\n".join("@" + x for x in unchecked))
        if excluded:
            blocks.append("Исключено: %d (уже куплены на Fragment)." % excluded)
        if blocks:
            text = "%s\nПроверено %d, по t.me свободно %d.\n\n%s" % (title, done, len(found), "\n\n".join(blocks))
        else:
            text = "%s\nПроверено %d, свободных не нашлось. Попробуй ещё раз." % (title, done)
        if limited:
            text += "\n\nt.me ограничивает запросы, поиск остановлен. Попробуй через несколько минут: бот сам замедлился."
        save_state()
        if not (mid and edit_text(chat_id, mid, text, "result")):
            send(chat_id, text, "result")
    finally:
        busy.discard(chat_id)


def start_search(chat_id, title, make_next, n, rank=None):
    if chat_id in busy:
        send(chat_id, "Поиск уже идёт, дождись результата.")
        return
    wait = COOLDOWN - (time.time() - last_run.get(chat_id, 0))
    if wait > 0:
        send(chat_id, "Подожди ещё %d сек." % int(wait))
        return
    last_run[chat_id] = time.time()
    last_job[chat_id] = (title, make_next, n, rank)
    busy.add(chat_id)
    threading.Thread(target=search, args=(chat_id, title, make_next, n, rank), daemon=True).start()


def launch(chat_id, kind, length=None, n=DEFAULT_N):
    s = cfg(chat_id)
    L = length or s["length"]
    title = ("Красивые" if kind == "find" else "Случайные") + " по %d" % L

    def factory():
        return lambda: make_name(kind, L, s["norep"], s["charset"])

    start_search(chat_id, title, factory, n)


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
        send(chat_id, "Слежу за:\n" + "\n".join("@" + n for n in names))
    else:
        send(chat_id, "Слежек нет. Добавь: /watch abcde")


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
             "\n\nСледить: /watch имя. Убрать: /unfav имя.")
    else:
        send(chat_id, "Избранное пусто. Нажми ⭐ под найденными юзами.")


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
            time.sleep(current_delay())
            if res:
                time.sleep(10)
                if check(name) is True:  # перепроверка против ложной тревоги
                    code, raw = fragment_status(name)
                    if code in ("sold", "taken"):
                        continue  # он на Fragment как занятый коллекционный, продолжаем следить
                    with lock:
                        chats = list(state["watches"].pop(name, []))
                    save_state()
                    note = ""
                    if code in ("listed", "other"):
                        note = "\n🛒 Но он на Fragment: %s (fragment.com/username/%s). Обычным способом его не занять." % (raw, name)
                    for c in chats:
                        send(c, "🔔 @%s похоже освободился! Проверь и займи как можно быстрее.%s" % (name, note))
        time.sleep(WATCH_INTERVAL)


def auto_worker():
    while True:
        time.sleep(AUTO_INTERVAL)
        with lock:
            chats = [int(c) for c, s in state["settings"].items() if s.get("auto")]
        for chat_id in chats:
            try:
                if not allowed(chat_id) or (maintenance_on() and not is_admin(chat_id)):
                    continue
                s = cfg(chat_id)
                found, done, limited = scan(
                    lambda: make_name("find", s["length"], s["norep"], s["charset"]), AUTO_N)
                if FRAGMENT_CHECK and found:
                    free_names, listed, unchecked, excluded = fragment_split(found)
                    found = free_names + unchecked
                with lock:
                    sent = state["sent"].setdefault(str(chat_id), [])
                    new = [x for x in found if x not in sent]
                    sent.extend(new)
                    del sent[:-500]
                save_state()
                if new:
                    last_found[chat_id] = list(new)
                    send(chat_id, "🔔 Автопоиск, новые похоже свободные (%d):\n%s" % (
                        len(new), "\n".join("@" + x for x in new)), "result")
            except Exception as e:
                logerr("auto error: %s" % e)


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
            logerr("ping error: %s" % e)
        time.sleep(PING_INTERVAL)


def note_conflict():
    """409 Conflict = кто-то ещё читает сообщения этого же токена. Если это долго, предупреждаем админа."""
    now = time.time()
    conflicts.append(now)
    recent = [t for t in conflicts if now - t < CONFLICT_WINDOW]
    if len(recent) >= CONFLICT_COUNT and now - last_conflict_alert[0] > CONFLICT_COOLDOWN:
        last_conflict_alert[0] = now
        for a in ADMIN_IDS:
            send(a, "⚠️ Похоже, твой токен используют ещё где-то (ошибка 409 повторяется %d раз за %d мин).\n\n"
                    "Если ты только что обновлял бота на Render, подожди пару минут. "
                    "Если нет, сделай /revoke в @BotFather и вставь новый токен в Render -> Environment (BOT_TOKEN)."
                    % (len(recent), CONFLICT_WINDOW // 60))


# ---------- админка ----------

def fmt_uptime():
    sec = int(time.time() - STARTED)
    return "%dч %dм" % (sec // 3600, (sec % 3600) // 60)


def admin_stats_text():
    with lock:
        st = dict(state["stats"])
        users = dict(state["users"])
        day = sum(1 for u in users.values() if time.time() - u.get("last", 0) < 86400)
        return (
            "Статистика бота:\nПользователей: %d (за сутки: %d)\nПроверено: %d\nНайдено свободных: %d\n"
            "Слежек: %d\nЗанятых в памяти: %d\nЗабанено: %d\nЗакрытый доступ: %s\nОбслуживание: %s\nСкорость: пауза %.1f сек, лимитов t.me: %d\nРаботаю: %s" % (
                len(users), day, st["checked"], st["found"], len(state["watches"]), len(taken),
                len(state["banned"]), "вкл" if state["private"] else "выкл",
                "вкл" if state["maintenance"] else "выкл", speed["delay"], speed["limits"], fmt_uptime()))


def admin_users_text():
    with lock:
        items = sorted(state["users"].items(), key=lambda kv: kv[1].get("last", 0), reverse=True)[:25]
        total = len(state["users"])
    if not items:
        return "Пользователей пока нет."
    lines = ["%s · %d запросов · %s" % (uid, u.get("n", 0), ago(u.get("last", 0))) for uid, u in items]
    return "Пользователи (последние %d из %d):\n%s" % (len(items), total, "\n".join(lines))


def do_broadcast(admin_id, text):
    with lock:
        ids = [int(i) for i in state["users"].keys() if int(i) not in state["banned"]]
    ok = 0
    for uid in ids:
        if send(uid, text):
            ok += 1
        time.sleep(0.06)
    send(admin_id, "Рассылка завершена: доставлено %d из %d." % (ok, len(ids)))


def do_restart(chat_id):
    send(chat_id, "♻ Перезапускаюсь, вернусь через пару секунд...")
    save_state()
    time.sleep(1)
    os._exit(0)


def admin_command(chat_id, cmd, parts, text):
    if cmd == "/admin":
        send(chat_id, "🛠 Админ-панель\n\n" + ADMIN_HELP, "admin")
    elif cmd == "/broadcast":
        body = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
        if not body:
            send(chat_id, "Напиши так: /broadcast текст сообщения")
            return
        send(chat_id, "Начинаю рассылку...")
        threading.Thread(target=do_broadcast, args=(chat_id, body), daemon=True).start()
    elif cmd in ("/ban", "/unban"):
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            send(chat_id, "Напиши так: %s 123456789" % cmd)
            return
        uid = int(parts[1])
        if is_admin(uid):
            send(chat_id, "Админа банить нельзя.")
            return
        with lock:
            if cmd == "/ban":
                if uid not in state["banned"]:
                    state["banned"].append(uid)
            elif uid in state["banned"]:
                state["banned"].remove(uid)
        save_state()
        send(chat_id, "ID %d %s." % (uid, "заблокирован" if cmd == "/ban" else "разблокирован"))
    elif cmd == "/users":
        send(chat_id, admin_users_text())
    elif cmd == "/rare":
        n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else DEFAULT_N
        launch_rare(chat_id, max(1, min(n, MAX_N)))
    elif cmd == "/maintenance":
        bits = text.split(None, 2)
        mode = bits[1].lower() if len(bits) > 1 else ""
        if mode == "on":
            set_maintenance(True, bits[2] if len(bits) > 2 else "")
            send(chat_id, "🛠 Режим обслуживания включён. Остальным бот отвечает:\n\n" + maint_message())
        elif mode == "off":
            set_maintenance(False)
            send(chat_id, "Режим обслуживания выключен, бот снова работает для всех.")
        else:
            send(chat_id, "Напиши так: /maintenance on [текст для пользователей] или /maintenance off")


def admin_callback(cb_id, chat_id, mid, action):
    if action == "open":
        answer(cb_id)
        send(chat_id, "🛠 Админ-панель\n\n" + ADMIN_HELP, "admin")
    elif action == "open2":
        answer(cb_id)
        edit_keyboard(chat_id, mid, "admin")
    elif action == "stats":
        answer(cb_id)
        send(chat_id, admin_stats_text())
    elif action == "users":
        answer(cb_id)
        send(chat_id, admin_users_text())
    elif action == "private":
        with lock:
            state["private"] = not state["private"]
            now = state["private"]
        save_state()
        answer(cb_id, "Закрытый доступ: %s" % ("вкл" if now else "выкл"))
        edit_keyboard(chat_id, mid, "admin")
    elif action == "rare":
        answer(cb_id)
        launch_rare(chat_id)
    elif action == "maint":
        now = not maintenance_on()
        set_maintenance(now)
        answer(cb_id, "Обслуживание: %s" % ("вкл" if now else "выкл"))
        edit_keyboard(chat_id, mid, "admin")
    elif action == "bc":
        answer(cb_id)
        send(chat_id, "Рассылка: напиши /broadcast текст, и бот отправит его всем пользователям.")
    elif action == "ban":
        answer(cb_id)
        send(chat_id, "Бан: /ban ID и /unban ID. ID пользователей смотри в списке «Юзеры».")
    elif action == "clear":
        with lock:
            taken.clear()
        save_state()
        answer(cb_id, "Кэш занятых очищен")
    elif action == "errors":
        answer(cb_id)
        send(chat_id, "Последние ошибки:\n" + ("\n".join(errlog) if errlog else "ошибок нет"))
    elif action == "restart":
        answer(cb_id)
        edit_keyboard(chat_id, mid, "confirm")
    elif action == "restart_yes":
        answer(cb_id, "Перезапуск...")
        threading.Thread(target=do_restart, args=(chat_id,), daemon=True).start()
    else:
        answer(cb_id)


# ---------- обработка сообщений ----------

def send_stats(chat_id):
    s = cfg(chat_id)
    send(chat_id,
         "Твои настройки:\nДлина: %d\nБез повторов: %s\nСимволы: %s\nАвтопоиск: %s\n"
         "Слежек: %d\nИзбранное: %d" % (
             s["length"], "да" if s["norep"] else "нет", s["charset"],
             "вкл" if s["auto"] else "выкл", len(my_watches(chat_id)), len(get_favs(chat_id))))


def handle(msg):
    chat_id = msg["chat"]["id"]
    touch(chat_id)

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
    if maintenance_on() and not is_admin(chat_id):
        send(chat_id, maint_message())
        return

    if cmd in ("/admin", "/broadcast", "/ban", "/unban", "/users", "/maintenance", "/rare"):
        if is_admin(chat_id):
            admin_command(chat_id, cmd, parts, text)
        return

    s = cfg(chat_id)

    if cmd in ("/start", "/help"):
        send(chat_id, HELP + ("\n\n" + ADMIN_HELP if is_admin(chat_id) else ""), "main")
    elif cmd == "/menu":
        send(chat_id, "Выбери:", "main")
    elif cmd == "/check":
        name = parts[1].lstrip("@").lower() if len(parts) > 1 else ""
        if not valid(name):
            send(chat_id, "Напиши так: /check abcde (5-32 символа, латиница, цифры, _)")
            return
        res = check(name)
        if res is None:
            send(chat_id, "Не удалось проверить, попробуй позже.")
        else:
            code, raw = fragment_status(name)
            send(chat_id, "@%s - %s\n%s" % (name, "похоже свободен" if res else "занят", frag_text(name, code, raw)))
    elif cmd in ("/fragment", "/fr"):
        names = [w.lstrip("@").lower() for w in parts[1:6] if valid(w.lstrip("@").lower())]
        if not names:
            send(chat_id, "Напиши так: /fragment имя (можно до 5 имён через пробел)")
            return
        lines = []
        for nm in names:
            code, raw = fragment_status(nm)
            lines.append("@%s\n%s" % (nm, frag_text(nm, code, raw)))
            time.sleep(FRAG_DELAY)
        send(chat_id, "\n\n".join(lines))
    elif cmd in ("/find", "/random"):
        length, n = parse_num_args(parts)
        launch(chat_id, "find" if cmd == "/find" else "rand", length, n)
    elif cmd == "/mask":
        mask = parts[1].lstrip("@").lower() if len(parts) > 1 else ""
        if (not re.fullmatch(r"[a-z?][a-z0-9_?]{4,31}", mask)) or mask.endswith("_"):
            send(chat_id, "Напиши так: /mask a??ie (? = любая буква, минимум 5 символов)")
            return
        if "?" not in mask:
            send(chat_id, "В маске нет «?». Для одного юза используй /check.")
            return
        n = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else DEFAULT_N
        start_search(chat_id, "Маска " + mask, lambda: mask_namer(mask), max(1, min(n, MAX_N)))
    elif cmd == "/words":
        custom = [w.lstrip("@").lower() for w in parts[1:] if valid(w.lstrip("@").lower())]
        if custom:
            names = custom[:MAX_N]
            start_search(chat_id, "Слова", words_factory(names, False), len(names))
        else:
            start_search(chat_id, "Слова", words_factory(WORDS, True), DEFAULT_N)
    elif cmd == "/word":
        word = translit(parts[1]) if len(parts) > 1 else ""
        if not word or len(word) > 20:
            send(chat_id, "Напиши так: /word king [N] - варианты юзернейма из слова (можно русскими буквами).")
            return
        cands = word_variants(word)
        if not cands:
            send(chat_id, "Из этого слова не получается допустимых юзернеймов.")
            return
        n = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else DEFAULT_N
        n = max(1, min(n, MAX_N, len(cands)))
        start_search(chat_id, "Слово «%s»" % word, word_factory(word), n)
    elif cmd == "/len":
        if len(parts) > 1 and parts[1].isdigit() and MIN_LEN <= int(parts[1]) <= MAX_LEN:
            s["length"] = int(parts[1])
            save_state()
            send(chat_id, "Длина: %d букв." % s["length"])
        else:
            send(chat_id, "Напиши так: /len 8 (от %d до %d)" % (MIN_LEN, MAX_LEN))
    elif cmd == "/norep":
        if len(parts) > 1 and parts[1].lower() in ("on", "off"):
            s["norep"] = parts[1].lower() == "on"
            save_state()
            send(chat_id, "Без повторяющихся букв: %s." % ("вкл" if s["norep"] else "выкл"))
        else:
            send(chat_id, "Напиши так: /norep on или /norep off")
    elif cmd == "/charset":
        if len(parts) > 1 and parts[1].lower() in ("letters", "alnum"):
            s["charset"] = parts[1].lower()
            save_state()
            send(chat_id, "Символы для случайных: %s." % ("только буквы" if s["charset"] == "letters" else "буквы и цифры"))
        else:
            send(chat_id, "Напиши так: /charset letters или /charset alnum")
    elif cmd == "/auto":
        if len(parts) > 1 and parts[1].lower() in ("on", "off"):
            s["auto"] = parts[1].lower() == "on"
            save_state()
            if s["auto"]:
                send(chat_id, "Автопоиск включён: раз в час проверяю %d красивых по %d букв и пишу только новые находки." % (AUTO_N, s["length"]))
            else:
                send(chat_id, "Автопоиск выключен.")
        else:
            send(chat_id, "Напиши так: /auto on или /auto off")
    elif cmd == "/watch":
        cmd_watch(chat_id, parts)
    elif cmd == "/unwatch":
        name = parts[1].lstrip("@").lower() if len(parts) > 1 else ""
        if remove_watch(chat_id, name):
            send(chat_id, "Больше не слежу за @%s." % name)
        else:
            send(chat_id, "Напиши так: /unwatch abcde (из твоих слежек)")
    elif cmd == "/watchlist":
        send_watchlist(chat_id)
    elif cmd == "/fav":
        if len(parts) > 1:
            name = parts[1].lstrip("@").lower()
            if valid(name):
                send(chat_id, "@%s %s" % (name, "добавлен в избранное." if add_fav(chat_id, name) else "уже в избранном."))
            else:
                send(chat_id, "Неверный юзернейм.")
        else:
            send_favs(chat_id)
    elif cmd == "/unfav":
        name = parts[1].lstrip("@").lower() if len(parts) > 1 else ""
        send(chat_id, "Убрал @%s из избранного." % name if remove_fav(chat_id, name) else "Такого нет в избранном.")
    elif cmd == "/stats":
        send_stats(chat_id)


def handle_callback(cb):
    cb_id = cb["id"]
    msg = cb.get("message")
    data = cb.get("data", "")
    if not msg:
        answer(cb_id)
        return
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    touch(chat_id)
    if not allowed(chat_id):
        answer(cb_id, "Нет доступа")
        return
    if maintenance_on() and not is_admin(chat_id):
        answer(cb_id, "Технические работы, попробуй позже")
        return
    s = cfg(chat_id)

    if data.startswith("adm:"):
        if is_admin(chat_id):
            admin_callback(cb_id, chat_id, mid, data[4:])
        else:
            answer(cb_id, "Нет доступа")
    elif data == "menu":
        answer(cb_id)
        edit_keyboard(chat_id, mid, "menu")
    elif data == "back":
        answer(cb_id)
        edit_keyboard(chat_id, mid, "main")
    elif data.startswith("len:"):
        n = int(data[4:]) if data[4:].isdigit() else 0
        if MIN_LEN <= n <= MAX_LEN:
            s["length"] = n
            save_state()
            answer(cb_id, "Длина: %d" % n)
            edit_keyboard(chat_id, mid, "menu")
        else:
            answer(cb_id)
    elif data in ("go:find", "go:rand"):
        answer(cb_id)
        launch(chat_id, data[3:])
    elif data == "again":
        answer(cb_id)
        job = last_job.get(chat_id)
        if job:
            start_search(chat_id, *job)
        else:
            send(chat_id, "Нечего повторять, сначала запусти поиск.")
    elif data == "favall":
        names = last_found.get(chat_id) or []
        added = sum(1 for n in names if add_fav(chat_id, n))
        answer(cb_id, "⭐ Добавлено в избранное: %d" % added)
    elif data == "list":
        answer(cb_id)
        send_watchlist(chat_id)
    elif data == "favs":
        answer(cb_id)
        send_favs(chat_id)
    elif data == "clear":
        clear_watches(chat_id)
        answer(cb_id, "Слежки удалены")
    else:
        answer(cb_id)


def keepalive():
    """Открытый порт для хостингов вроде Render + ответ на пинги."""
    port = os.environ.get("PORT")
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("0.0.0.0", int(port)), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def main():
    if not TOKEN:
        print("Нет токена: добавь переменную BOT_TOKEN в Render -> Environment", flush=True)
        return
    load_state()
    keepalive()
    for fn in (watcher, auto_worker, pinger, saver):
        threading.Thread(target=fn, daemon=True).start()
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
            logerr("poll error: %s" % e)
            if "409" in str(e):
                note_conflict()
            time.sleep(5)


if __name__ == "__main__":
    main()
