"""clip2vtf - картинка -> materials/<папка>/<имя>.vtf + .vmt.

Откуда брать картинку:
- Ctrl+V: скриншот, картинка из браузера ("Копировать изображение"), файл, скопированный в
  Проводнике, ссылка на картинку или путь к файлу текстом.
- Drag & drop в окно: файл из Проводника или картинка прямо из браузера.
- Перетащить файл на ярлык / clip2vtf.bat.

Имя подставляется само (галочка "Подставлять имя автоматически"): из имени файла, из описания
картинки на сайте (alt/title), из поискового запроса страницы (Google/Яндекс), из адреса картинки.
Хеши, номера и слова вроде images/download/unnamed выкидываются. Браузер кладёт alt и адрес
страницы в буфер вместе с картинкой (формат "HTML Format"), его читаем через WinAPI.

Горячие клавиши: Ctrl+V, Ctrl+O, Ctrl+S, Ctrl+Shift+S, Ctrl+Shift+C, Ctrl+E, Ctrl+Q, F1.
Ловятся по коду клавиши, поэтому работают и на русской раскладке.

Почему именно так (подробности - в NOTES.md):
- VTF пишется ТОЛЬКО версии 7.2. srctools 2.7.0 пишет битый заголовок для 7.0/7.1, а сервер
  этой сборки не читает 7.4/7.5 ("invalid minor version"). По умолчанию srctools пишет 7.5.
- reflectivity считается по картинке. Со значением по умолчанию (0,0,0) HDR пересвечивает.
- Размеры только степени двойки, иначе материал не рендерится на модели/браше.
- Результат проверяется декодером Valve (vtf2tga.exe), а не srctools: srctools читает свой же
  битый вывод без ошибок, поэтому проверять им бесполезно.
- .bz2 для FastDL либо создаётся заново вместе с файлом, либо старый удаляется: клиент всегда
  берёт .bz2 первым, старый .bz2 = старая текстура у игроков.
"""
import base64
import bz2
import hashlib
import html
import io
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageGrab, ImageTk

try:
    import numpy as np
except ImportError:  # без numpy: нет PSNR-проверки, фон убирается медленнее
    np = None

try:  # drag & drop; без пакета программа работает, просто без перетаскивания
    import tkinterdnd2
    from tkinterdnd2 import TkinterDnD
except ImportError:
    tkinterdnd2 = None

from srctools.vtf import VTF, VTFFlags, ImageFormats, FilterMode

class _ParallelFormatFuncs:
    """Обёртка над кодировщиком srctools: DXT-кадр режется на полосы по строкам (кратно 4 - блок
    DXT 4x4 кодируется независимо от соседей) и полосы сжимаются на всех ядрах. Libsquish в
    Cython-модуле srctools отпускает GIL, так что хватает потоков. Результат байт в байт тот же
    (проверено на 2048x1024 DXT1/DXT5), а 3.6 с -> 0.8 с на 6 ядрах. Всё, кроме save, - как было."""

    def __init__(self, base):
        from concurrent.futures import ThreadPoolExecutor
        self._base = base
        self._n = os.cpu_count() or 4
        self._pool = ThreadPoolExecutor(self._n, thread_name_prefix="dxt")

    def __getattr__(self, name):
        return getattr(self._base, name)

    def save(self, fmt, pixels, data, width, height):
        if fmt not in (ImageFormats.DXT1, ImageFormats.DXT3, ImageFormats.DXT5) \
                or width % 4 or height % 4 or width * height < 128 * 128:
            return self._base.save(fmt, pixels, data, width, height)
        n = max(1, min(self._n * 4, height // 4))
        rows = sorted({(i * height // n) // 4 * 4 for i in range(n)} | {height})
        spans = [(rows[i], rows[i + 1]) for i in range(len(rows) - 1)]
        base, stride = self._base, width * 4

        def job(span):
            y0, y1 = span
            buf = bytearray(fmt.frame_size(width, y1 - y0))
            base.save(fmt, pixels[y0 * stride:y1 * stride], buf, width, y1 - y0)
            return y0, buf

        out = memoryview(data).cast("B")
        for y0, buf in self._pool.map(job, spans):
            off = fmt.frame_size(width, y0) if y0 else 0
            out[off:off + len(buf)] = buf


import srctools.vtf as _srctools_vtf  # noqa: E402
if not isinstance(_srctools_vtf._format_funcs, _ParallelFormatFuncs):
    _srctools_vtf._format_funcs = _ParallelFormatFuncs(_srctools_vtf._format_funcs)

FROZEN = getattr(sys, "frozen", False)  # собранный PyInstaller'ом clip2vtf.exe
# Где лежит программа (рядом с ней - настройки, лог, исходники картинок) и откуда брать ресурсы
# (иконку): у exe из одного файла ресурсы распакованы во временную папку sys._MEIPASS.
HERE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
RES = getattr(sys, "_MEIPASS", HERE)


def _data_dir():
    """Папка для настроек/лога/исходников: рядом с программой (переносная), а если туда нельзя
    писать (Program Files) - %APPDATA%\\clip2vtf."""
    try:
        probe = os.path.join(HERE, ".clip2vtf_write_test")
        with open(probe, "w"):
            pass
        os.remove(probe)
        return HERE
    except OSError:
        d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "clip2vtf")
        os.makedirs(d, exist_ok=True)
        return d


DATA = os.environ.get("CLIP2VTF_DATA") or _data_dir()  # переменная - для тестов / своей папки
SETTINGS = os.path.join(DATA, "clip2vtf_settings.json")

# ---------------------------------------------------------------- языки
# Исходный язык текстов - русский: он и есть ключ перевода. lang/<код>.json = {русский текст:
# перевод}. Язык выбирается до построения всех надписей (они собираются при импорте), поэтому
# смена языка - перезапуск программы (App.set_language делает его сам).
LANGUAGES = [  # (код, название на самом языке)
    ("en", "English"), ("ru", "Русский"), ("uk", "Українська"), ("de", "Deutsch"),
    ("fr", "Français"), ("es", "Español"), ("pt", "Português (Brasil)"), ("pl", "Polski"),
    ("tr", "Türkçe"), ("zh", "简体中文"),
]
_WIN_LANG = {0x19: "ru", 0x22: "uk", 0x07: "de", 0x0C: "fr", 0x0A: "es", 0x16: "pt", 0x15: "pl",
             0x1F: "tr", 0x04: "zh", 0x23: "ru"}  # 0x23 - белорусский: ближе всего русский


def system_language():
    try:
        import ctypes
        lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return _WIN_LANG.get(lid & 0x3FF, "en")
    except Exception:
        return "en"


def _saved_language():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            code = json.load(f).get("lang")
        return code if code in dict(LANGUAGES) else None
    except Exception:
        return None


LANG = os.environ.get("CLIP2VTF_LANG") or _saved_language() or system_language()
_TR = {}


def load_language(code):
    global LANG, _TR
    LANG, _TR = code, {}
    if code == "ru":
        return
    for c in (code, "en"):  # чего нет в выбранном - по-английски, потом как есть (русский)
        try:
            with open(os.path.join(RES, "lang", c + ".json"), encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    if v:
                        _TR.setdefault(k, v)
        except (OSError, ValueError):
            pass


def tr(text):
    """Перевод строки интерфейса на выбранный язык. Внутренние значения настроек (варианты
    подгонки, форматов...) хранятся по-русски и переводятся только при показе."""
    return _TR.get(text, text)


load_language(LANG)
# В Segoe UI нет иероглифов: Windows подставляет для каждого знака свой запасной шрифт, и текст
# выходит вперемешку жирным и обычным. Для китайского - шрифт, где есть всё.
UI_FONT = "Microsoft YaHei UI" if LANG == "zh" else "Segoe UI"


def _steam_libraries():
    """Папки библиотек Steam: путь Steam из реестра + steamapps/libraryfolders.vdf."""
    roots = []
    try:
        import winreg
        for hive, key, name in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    roots.append(os.path.normpath(winreg.QueryValueEx(k, name)[0]))
            except OSError:
                pass
    except ImportError:
        pass
    libs = []
    for steam in roots:
        libs.append(steam)
        try:
            with open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), encoding="utf-8") as f:
                libs += [os.path.normpath(p.replace("\\\\", "\\"))
                         for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
        except OSError:
            pass
    out, seen = [], set()
    for p in libs:  # в реестре путь бывает в нижнем регистре - сравнивать без регистра
        if os.path.normcase(p) not in seen:
            seen.add(os.path.normcase(p))
            out.append(p)
    return out


def find_tf2():
    """Папка "Team Fortress 2" (с bin и tf) или ''."""
    for lib in _steam_libraries():
        d = os.path.join(lib, "steamapps", "common", "Team Fortress 2")
        if os.path.isdir(os.path.join(d, "tf")):
            return d
    return ""


TF2_DIR = find_tf2()
# Куда TF2 положил свои инструменты: vtf2tga.exe - декодер Valve для проверки результата. Он
# не входит в программу (это файл Valve), берётся из установленной игры.
GAME_TFS = []  # папки tf из настроек; App обновляет, чтобы найти vtf2tga и рядом с ними


def find_vtf2tga():
    dirs = [os.path.dirname(t.rstrip("\\/")) for t in GAME_TFS if t] + ([TF2_DIR] if TF2_DIR else [])
    for d in dirs:
        for sub in ("bin", os.path.join("bin", "x64")):
            p = os.path.join(d, sub, "vtf2tga.exe")
            if os.path.isfile(p):
                return p
    return ""


DEFAULTS = {
    # Сервер и клиент по умолчанию - один и тот же tf установленной игры (у большинства так).
    "tf_root": os.path.join(TF2_DIR, "tf") if TF2_DIR else "",
    "client_tf": os.path.join(TF2_DIR, "tf") if TF2_DIR else "",
    "subdir": "custom",
    "width": "авто",
    "height": "авто",
    "fit": "Растянуть",
    "fmt": "Авто",
    "shader": "VertexLitGeneric",
    "alpha_mode": "Авто",
    "decal_units": "128",
    # текстура
    "mips": True,
    "nolod": False,
    "aniso": False,
    "pixel_art": False,
    "clamp": False,
    "flip_h": False,
    "bg_remove": False,
    "trim": False,
    "bleed": True,
    # материал
    "nocull": False,
    "additive": False,
    "nodecal": False,
    "vertexcolor": False,
    # сохранение
    "auto_name": True,
    "autosave": False,
    "mirror_client": False,
    "make_bz2": False,
    "delete_stale_bz2": True,
    "verify": True,
    "confirm_overwrite": False,
    "unique_name": False,
    "save_source": False,
    "copy_path_after_save": False,
    "open_folder_after_save": False,
    "topmost": False,
    "send_hammer": False,
    "hammer_drop": True,
    "size_panel": True,
    # режим "текстура на браш": как положить картинку на грань (см. BRUSH_FITS и др.)
    "brush_mode": "Растянуть на всю грань (Fit)",
    "brush_scale": "0.25",
    "brush_align": "По центру",
    "brush_rotate": "0",
    "brush_luxel": "не менять",
    "hammer_opts_open": True,  # раскрыты ли настройки на панели поверх Hammer
    "lang": LANG,              # язык интерфейса (см. LANGUAGES), по умолчанию - язык Windows
    "hammer_mode": "Декаль (размер из настройки)",  # Apply decals - так попросил пользователь
}

TEXTURE_CHECKS = [
    ("mips", tr("Мипмапы"),
     tr("Уменьшенные копии текстуры для дальних расстояний. Без них текстура рябит и мерцает "
     "издалека. Выключай только для HUD и спрайтов, которые всегда видно вблизи. +33% к размеру.")),
    ("nolod", tr("Всегда полное качество (No LOD)"),
     tr("Игрок с низким качеством текстур в настройках всё равно увидит полное разрешение. "
     "Для надписей и мелкого текста, который иначе станет нечитаемым.")),
    ("aniso", tr("Анизотропная фильтрация"),
     tr("Текстура чётче под острым углом: пол, дорога, стена вдоль взгляда. Почти бесплатно.")),
    ("pixel_art", tr("Пиксель-арт (без сглаживания)"),
     tr("Масштабирует без размытия и включает point sampling: пиксели остаются чёткими квадратами. "
     "Лучше вместе с форматом BGRA8888, DXT размывает края блоков 4x4.")),
    ("clamp", tr("Не повторять по краям (Clamp)"),
     tr("Край текстуры не заворачивается на противоположную сторону. Убирает тонкую полоску чужого "
     "края у декалей, спрайтов и иконок. Для плитки, которая повторяется по стене, - выключи.")),
    ("flip_h", tr("Отразить по горизонтали"),
     tr("Зеркалит картинку. Если декаль или надпись в игре смотрит не в ту сторону.")),
    ("bg_remove", tr("Убрать однотонный фон"),
     tr("Делает прозрачным фон того же цвета, что в углах картинки (белый, чёрный, зелёный...). "
     "Убирается только фон, который касается краёв: такие же пятна внутри рисунка остаются.")),
    ("trim", tr("Обрезать пустые поля"),
     tr("Срезает полностью прозрачные края до масштабирования, чтобы рисунок занял всю текстуру. "
     "Удобно вместе с \"Убрать однотонный фон\" для стикеров и декалей.")),
    ("bleed", tr("Протянуть цвет под прозрачные пиксели"),
     tr("Невидимые пиксели получают цвет ближайшего видимого. Убирает белую или тёмную кайму "
     "вокруг прозрачных краёв, которая вылезает издалека (на мипмапах).")),
]
MATERIAL_CHECKS = [
    ("nocull", tr("$nocull - видно с обеих сторон"),
     tr("Для плоских моделей, листьев, табличек: без этого с обратной стороны полигон невидим.")),
    ("additive", tr("$additive - светящееся наложение"),
     tr("Цвет складывается с тем, что позади: чёрное становится прозрачным, светлое светится. "
     "Для огня, бликов, неона, голограмм. Альфа-канал для этого не нужен.")),
    ("nodecal", tr("$nodecal - без следов от пуль"),
     tr("На этой поверхности не остаются следы пуль и взрывов и не рисуются спреи. Для стен и брашей.")),
    ("vertexcolor", "$vertexcolor + $vertexalpha",
     tr("Цвет и прозрачность можно менять из карты или плагина (rendercolor / renderamt). "
     "Так сделаны стоковые декали TF2.")),
]
SAVE_CHECKS = [
    ("auto_name", tr("Подставлять имя автоматически"),
     tr("Имя берётся из имени файла, описания картинки на сайте, поискового запроса или адреса "
     "картинки; хеши и слова вроде images/download выкидываются. Выключи, чтобы имя не менялось "
     "(например, раз за разом заменять одну и ту же текстуру).")),
    ("autosave", tr("Сохранять сразу после вставки"),
     tr("Ctrl+V или перетаскивание сразу создают VTF + VMT с текущими настройками, без Ctrl+S. "
     "Для потока из многих картинок подряд.")),
    ("hammer_drop", tr("Перетаскивание картинки прямо в Hammer++"),
     tr("Тащишь картинку из браузера или Проводника на окно Hammer - поверх него появляется "
     "полупрозрачная зона. Отпускаешь над стеной в 3D-виде - программа делает материал, "
     "включает нужный инструмент Hammer и кликает в точку, где ты отпустил: картинка "
     "появляется на стене декалью, текстурой грани или оверлеем (\"В Hammer как\" ниже). "
     "После этого снова включается Selection. Материал с тем же именем не перезаписывается "
     "(добавляется _2, _3).")),
    ("size_panel", tr("Ползунок размера декали поверх Hammer"),
     tr("Когда в Hammer выделена декаль (infodecal), в его заголовке появляется панель "
     "\"Декаль: [ползунок] [число] юн.\". Размер тянется ползунком, крутится колесом или "
     "вписывается числом (Enter) и сразу применяется к выделенной декали (Ctrl+Z в Hammer "
     "отменяет). Работает для декалей, сделанных clip2vtf. Заодно это размер следующих картинок "
     "(то же поле \"Декаль, ширина в юнитах\"). Кнопка \"Настройки\" на панели раскрывает все "
     "галочки и списки вкладок Текстура и Материал: изменение сразу пересобирает выделенную "
     "декаль (и действует на следующие картинки).")),
    ("send_hammer", tr("Отправлять в Hammer++ после сохранения"),
     tr("Сохранённый материал сразу становится текущей текстурой в открытом Hammer++ - остаётся "
     "применить его к грани. Программа сама открывает браузер текстур Hammer, вводит имя в фильтр "
     "и выбирает плитку. Материал при этом всегда копируется в клиентский tf: Hammer читает "
     "оттуда. Вручную - кнопка \"В Hammer\" или Ctrl+H.")),
    ("mirror_client", tr("Копировать ещё в клиент TF2"),
     tr("Кладёт копию в tf клиента (путь ниже). Нужно, чтобы увидеть материал у себя в игре и в "
     "Hammer: они не видят серверную папку.")),
    ("make_bz2", tr("Создавать .bz2 для FastDL"),
     tr("Рядом с серверной копией пишутся сжатые .vtf.bz2 / .vmt.bz2: игроки качают их быстрее. "
     "Клиентской копии .bz2 не нужны.")),
    ("delete_stale_bz2", tr("Удалять устаревший .bz2"),
     tr("Если .bz2 не создаётся, а старый лежит рядом, он удаляется. Иначе игроки скачают старую "
     "версию текстуры: клиент всегда берёт .bz2 первым.")),
    ("verify", tr("Проверять результат через vtf2tga"),
     tr("После записи файл читается декодером Valve и сравнивается с картинкой. Если он битый или "
     "сильно испорчен сжатием, строка статуса станет красной.")),
    ("confirm_overwrite", tr("Спрашивать перед перезаписью"),
     tr("Если материал с таким именем уже есть, сначала спросить.")),
    ("unique_name", tr("Не перезаписывать: добавлять _2, _3..."),
     tr("Если имя занято, к нему добавляется номер. Старый материал остаётся как был.")),
    ("save_source", tr("Сохранять исходник в materialsrc/"),
     tr("Оригинал картинки в полном размере ляжет в tf/materialsrc/<папка>/<имя>.png, как хранит "
     "исходники Valve. Клиентам не отправляется, нужен, чтобы потом пересобрать текстуру.")),
    ("copy_path_after_save", tr("Копировать путь материала после сохранения"),
     tr("В буфер попадёт путь вида папка/имя - вставлять в Hammer, в конфиг или в код плагина.")),
    ("open_folder_after_save", tr("Открывать папку после сохранения"),
     tr("После каждого сохранения открывается окно Проводника с материалом.")),
    ("topmost", tr("Поверх всех окон"),
     tr("Окно программы не прячется за браузером - удобно перетаскивать картинки.")),
]
# Windows virtual-key коды не зависят от раскладки. Tk 8.6 на русской раскладке присылает
# Ctrl+V с keysym "Cyrillic_em", и его собственные <<Paste>>/<<Copy>> тогда не срабатывают.
VK = {"V": 86, "S": 83, "O": 79, "E": 69, "Q": 81, "C": 67, "X": 88, "A": 65, "H": 72}
HELP = tr("""Горячие клавиши

Ctrl+V\t\tвставить картинку из буфера
Ctrl+O\t\tоткрыть файл картинки
Ctrl+S\t\tсохранить VTF + VMT
Ctrl+Shift+S\tсохранить как... (папка внутри materials)
Ctrl+Shift+C\tкопировать путь материала ($basetexture)
Ctrl+E\t\tоткрыть папку с материалом
Ctrl+H\t\tсделать материал текущей текстурой в Hammer++
Ctrl+Q, Alt+F4\tвыход
F1\t\tэта справка

В полях ввода Ctrl+V/C/X/A работают с текстом как обычно
(если в буфере картинка, Ctrl+V всё равно вставит её).

Картинку можно перетащить в окно: файл из Проводника или
картинку прямо из браузера. Ссылку на картинку можно
скопировать - Ctrl+V её скачает.

Имя подставляется само: из имени файла, описания картинки
на сайте, поискового запроса или адреса картинки.

Картинку можно тащить прямо на окно Hammer++: поверх него
появится оранжевая зона. Отпусти над стеной в 3D-виде -
картинка встанет декалью (Apply decals). Её размер задаёт
"Декаль, ширина в юнитах" на вкладке "Материал".""")
SIZES = ["авто", "16", "32", "64", "128", "256", "512", "1024", "2048", "4096"]
AUTO_CAP = 2048
FITS = ["Растянуть", "Вписать (прозрачные поля)", "Обрезать по центру"]
FORMATS = {
    "Авто": None,  # DXT5 если есть прозрачность, иначе DXT1
    "DXT1 (без альфы, самый лёгкий)": ImageFormats.DXT1,
    "DXT5 (с альфой)": ImageFormats.DXT5,
    "BGRA8888 (без сжатия, с альфой)": ImageFormats.BGRA8888,
    "BGR888 (без сжатия)": ImageFormats.BGR888,
}
NO_ALPHA_FORMATS = (ImageFormats.DXT1, ImageFormats.BGR888)
DECAL = "Декаль (infodecal)"
SHADERS = ["VertexLitGeneric", "LightmappedGeneric", "UnlitGeneric", DECAL]
SHADER_HINTS = {
    "VertexLitGeneric": tr("для моделей (.mdl)"),
    "LightmappedGeneric": tr("для брашей: стены, пол"),
    "UnlitGeneric": tr("без освещения: светится ровно, для экранов и HUD"),
    DECAL: tr("декаль на стене; размер задаётся ниже"),
}
# Размер декали в мире = пиксели текстуры x $decalscale (1 пиксель = 1 юнит при 1.0).
# Так же сделаны стоковые декали TF2, например passtime/pass_jumppad_decal.vmt:
# "LightMappedGeneric" + "$decal" 1 + "$decalscale" 0.25 + "$translucent" 1.
ALPHA_MODES = ["Авто", "Нет", "$translucent", "$alphatest"]
# Как ставить перетащенную в Hammer картинку -> команда инструмента Hammer (WM_COMMAND).
# Коды из таблицы строк стокового hammer_dll.dll: 33107 "Apply overlays [Shift+O]",
# 33008 "Apply decals [Shift+D]", 32913 "Texture application"; порядок кнопок панели
# инструментов Hammer++ совпадает с ним. info_overlay в Hammer можно растягивать за углы
# в 3D-виде, у infodecal размер задаёт материал, текстура на браш ложится на грань под курсором
# (и по желанию растягивается на неё кнопкой Fit окна Face Edit, см. _hammer_texture_face).
# Внутренние значения - как в сохранённых настройках; новый режим вставлен в середину списка.
HAMMER_MODES = ["Декаль (размер из настройки)", "Текстура на браш (грань под курсором)",
                "Оверлей (растягивается мышью)"]
TOOL_DECAL, TOOL_FACE, TOOL_OVERLAY = 33008, 32913, 33107
# Режим "текстура на браш": как положить картинку на грань (окно Face Edit Hammer).
BRUSH_FITS = ["Растянуть на всю грань (Fit)", "Заполнить грань, сохранив пропорции",
              "Вписать целиком, сохранив пропорции", "Свой масштаб", "Масштаб Hammer (плиткой)"]
BRUSH_ALIGNS = ["По центру", "Как есть", "Влево", "Вправо", "Вверх", "Вниз"]
BRUSH_ROTATES = ["0", "90", "180", "270"]
BRUSH_LUXELS = ["не менять", "4", "8", "16", "32", "64"]
HAMMER_TOOL = {HAMMER_MODES[0]: TOOL_DECAL, HAMMER_MODES[1]: TOOL_FACE, HAMMER_MODES[2]: TOOL_OVERLAY}

# Раскладка вкладок. Строка = ключ настройки, кортеж ("note", текст) = пояснение.
FIELD_DEFS = {  # ключ -> (подпись, значения списка; None = поле ввода)
    "width": (tr("Ширина:"), SIZES),
    "height": (tr("Высота:"), SIZES),
    "fit": (tr("Подгонка:"), FITS),
    "fmt": (tr("Формат:"), list(FORMATS)),
    "shader": (tr("Шейдер:"), SHADERS),
    "alpha_mode": (tr("Прозрачность:"), ALPHA_MODES),
    "decal_units": (tr("Декаль, ширина\nв юнитах:"), None),
    "hammer_mode": (tr("В Hammer как:"), HAMMER_MODES),
    "brush_mode": (tr("Текстура на браш:"), BRUSH_FITS),
    "brush_scale": (tr("Свой масштаб\n(юнитов на пиксель):"), None),
    "brush_align": (tr("Выравнивание:"), BRUSH_ALIGNS),
    "brush_rotate": (tr("Поворот, градусы:"), BRUSH_ROTATES),
    "brush_luxel": (tr("Лайтмапа (luxel):"), BRUSH_LUXELS),
}
CHECK_DEFS = {k: (t, d) for k, t, d in TEXTURE_CHECKS + MATERIAL_CHECKS + SAVE_CHECKS}
TAB_LAYOUT = {
    "tex": ["width", "height", "fit", "fmt",
            ("note", tr("Размер \"авто\" = ближайшая степень двойки (до 2048). Формат \"Авто\": DXT1, "
                     "если нет прозрачности, иначе DXT5."))]
           + [k for k, _, _ in TEXTURE_CHECKS],
    "mat": ["shader", "alpha_mode", "decal_units",
            ("note", tr("Прозрачность \"Авто\": $translucent для плавной альфы, $alphatest для резкой "
                     "(только 0/255). Для масштаба: игрок - около 83 юнитов в высоту."))]
           + [k for k, _, _ in MATERIAL_CHECKS],
    "hammer": ["hammer_drop", "hammer_mode", "size_panel",
             ("note", tr("Декаль (Apply decals): размер задаёт \"Декаль, ширина в юнитах\" на вкладке "
                      "Материал, мышью в Hammer она не растягивается. Текстура на браш ложится на "
                      "грань, над которой отпустил картинку. Оверлей (Apply overlays) можно растянуть "
                      "за углы в 3D-виде."))]
            + ["send_hammer",
               ("note", tr("Текстура на браш: \"Заполнить\" растягивает картинку без искажения, пока "
                        "она не закроет всю грань (края обрезаются), \"Вписать целиком\" - пока вся "
                        "картинка не поместится (остаток грани заполнится её повтором). Свой масштаб: "
                        "0.25 - как у стандартных текстур, 1 - один пиксель на юнит. Лайтмапа: меньше "
                        "число - чётче тени на картинке, но дороже для карты (по умолчанию 16).")),
               "brush_mode", "brush_scale", "brush_align", "brush_rotate", "brush_luxel"],
    "save": [k for k, _, _ in SAVE_CHECKS if k not in ("hammer_drop", "size_panel", "send_hammer")],
}
ALL_SETTINGS = [k for items in TAB_LAYOUT.values() for k in items if isinstance(k, str)]
DEFAULT_FAVORITES = ["fit", "fmt", "shader", "decal_units", "bg_remove", "trim", "mirror_client",
                     "auto_name", "send_hammer"]
# Панель поверх Hammer, кнопка "Настройки": всё, что меняет саму картинку и материал декали
# (шейдер не нужен - декаль всегда LightmappedGeneric + $decal). Те же переменные, что в окне.
HAMMER_OPT_COLUMNS = [
    (tr("Текстура"), ["width", "height", "fit", "fmt"] + [k for k, _, _ in TEXTURE_CHECKS]),
    (tr("Материал"), ["alpha_mode"] + [k for k, _, _ in MATERIAL_CHECKS]),
]
REBUILD_KEYS = [k for _, keys in HAMMER_OPT_COLUMNS for k in keys]
FIELD_HINTS = {
    "width": tr("Ширина текстуры в пикселях. \"авто\" - ближайшая степень двойки (до 2048). "
             "Размер декали в юнитах от этого не меняется: его задаёт ползунок выше."),
    "height": tr("Высота текстуры в пикселях. \"авто\" - по пропорциям картинки."),
    "fit": tr("Как вписать картинку в текстуру другой пропорции: растянуть, вписать с прозрачными "
           "полями или обрезать по центру."),
    "fmt": tr("Сжатие. Авто: DXT1, если нет прозрачности, иначе DXT5. BGRA8888 - без потерь, "
           "но в 4-8 раз тяжелее."),
    "alpha_mode": tr("Авто: $translucent для плавной прозрачности, $alphatest для резкой (только 0/255)."),
}
MIN_PSNR = 30.0
BG_TOLERANCE = 40  # макс. отличие канала от цвета фона, 0..255


# ---------------------------------------------------------------- обработка картинки

def pot_nearest(x):
    return 2 ** max(0, round(math.log2(max(1, x))))


def target_size(w, h, sw, sh):
    """sw/sh - строка из SIZES. 'авто' = ближайшая степень двойки (с сохранением пропорций,
    если вторая сторона задана)."""
    if sw != "авто" and sh != "авто":
        return int(sw), int(sh)
    if sw != "авто":
        W = int(sw)
        return W, min(4096, pot_nearest(h * W / w))
    if sh != "авто":
        H = int(sh)
        return min(4096, pot_nearest(w * H / h)), H
    W, H = pot_nearest(w), pot_nearest(h)
    while max(W, H) > AUTO_CAP:
        W, H = max(1, W // 2), max(1, H // 2)
    return W, H


def remove_background(img, tol=BG_TOLERANCE):
    """Прозрачным становится фон цвета углов, связанный с краями картинки."""
    if np is None:
        out = img.copy()
        for xy in ((0, 0), (img.width - 1, 0), (0, img.height - 1), (img.width - 1, img.height - 1)):
            if out.getpixel(xy)[3] > 0:
                ImageDraw.floodfill(out, xy, (0, 0, 0, 0), thresh=tol)
        return out
    a = np.asarray(img).astype(np.int16)
    h, w = a.shape[:2]
    corners = [a[0, 0], a[0, w - 1], a[h - 1, 0], a[h - 1, w - 1]]
    close = np.zeros((h, w), bool)
    dist = np.full((h, w), 255, np.int16)
    for c in corners:
        if c[3] == 0:
            continue
        d = np.abs(a[..., :3] - c[:3]).max(axis=2)
        dist = np.minimum(dist, d)
        close |= d <= tol
    close &= a[..., 3] > 0
    if not close.any():
        return img
    bg = border_connected(close)
    out = a.copy()
    out[bg, 3] = 0
    # мягкий край: кольцо вокруг фона получает альфу по близости к цвету фона (без кольца
    # от сглаживания останется ореол фона вокруг рисунка)
    ring = dilate4(bg) & ~bg
    soft = np.clip((dist[ring] - tol) * 255 // max(1, tol), 0, 255)
    out[ring, 3] = np.minimum(out[ring, 3], soft)
    return Image.fromarray(out.astype(np.uint8), "RGBA")


# Без scipy: в exe она одна весила 69 МБ из 150 и замедляла запуск, а нужны от неё были три
# функции. Здесь - numpy-замены; фон и кольцо совпадают с scipy.ndimage побитово (проверено).
def _grow_runs(reached, mask):
    """Вдоль строк: непрерывная серия пикселей mask, в которой достигнут хоть один, - вся
    достигнута. (Для столбцов - вызвать на транспонированных массивах.)"""
    h, w = mask.shape
    m = mask.ravel()
    starts = m.copy()
    starts[1:] &= ~m[:-1]
    starts.reshape(h, w)[:, 0] = mask[:, 0]   # новая строка - всегда новая серия
    run = np.cumsum(starts) * m               # номер серии, 0 - не mask
    hit = np.zeros(int(run.max()) + 1, bool)
    hit[run[reached.ravel() & m]] = True
    hit[0] = False
    return hit[run].reshape(h, w)


def border_connected(mask):
    """Пиксели mask, связанные (4-связно) с краем картинки - то же, что компоненты
    scipy.ndimage.label, касающиеся края. Строки и столбцы по очереди, пока не перестанет
    меняться: каждый проход проводит путь через один поворот, обычно хватает нескольких."""
    reached = np.zeros_like(mask)
    reached[0] |= mask[0]
    reached[-1] |= mask[-1]
    reached[:, 0] |= mask[:, 0]
    reached[:, -1] |= mask[:, -1]
    while True:
        new = _grow_runs(reached, mask)
        new = _grow_runs(new.T.copy(), mask.T.copy()).T
        if (new == reached).all():
            return new
        reached = new


def dilate4(m):
    """scipy.ndimage.binary_dilation со структурой по умолчанию (крест 3x3)."""
    out = m.copy()
    out[1:] |= m[:-1]
    out[:-1] |= m[1:]
    out[:, 1:] |= m[:, :-1]
    out[:, :-1] |= m[:, 1:]
    return out


def bleed_colors(img):
    """RGB полностью прозрачных пикселей - из окружающих видимых (против каймы на мипах).
    Pull-push: пирамида средних по видимым пикселям, дыры заполняются с более грубого уровня.
    Видимые пиксели не меняются."""
    if np is None:
        return img
    a = np.asarray(img)
    hidden = a[..., 3] == 0
    if not hidden.any() or hidden.all():
        return img
    w = (~hidden).astype(np.float32)
    levels = [(a[..., :3].astype(np.float32) * w[..., None], w)]
    while levels[-1][1].shape != (1, 1):
        c, wt = levels[-1]
        h, wd = wt.shape
        ph, pw = (h > 1) * (h % 2), (wd > 1) * (wd % 2)
        if ph or pw:
            c = np.pad(c, ((0, ph), (0, pw), (0, 0)))
            wt = np.pad(wt, ((0, ph), (0, pw)))
        fy, fx = (2 if h > 1 else 1), (2 if wd > 1 else 1)

        def half(x):  # сумма блоков fy x fx срезами - в разы быстрее reshape().sum()
            x = x[0::fy] + x[1::fy] if fy == 2 else x
            return x[:, 0::fx] + x[:, 1::fx] if fx == 2 else x
        levels.append((half(c), half(wt)))
    color = None
    for c, wt in reversed(levels[1:]):  # от грубого к точному, кроме полного размера
        own = c / np.maximum(wt, 1e-6)[..., None]
        if color is not None:
            fy = 2 if wt.shape[0] > color.shape[0] else 1
            fx = 2 if wt.shape[1] > color.shape[1] else 1
            up = np.repeat(np.repeat(color, fy, 0), fx, 1)[:wt.shape[0], :wt.shape[1]]
            own = np.where((wt > 0)[..., None], own, up)
        color = own
    # полный размер: видимые остаются как есть, скрытым - цвет с уровня выше (без полноразмерных
    # промежуточных массивов - это была основная часть времени)
    ys, xs = np.nonzero(hidden)
    fy = 2 if a.shape[0] > color.shape[0] else 1
    fx = 2 if a.shape[1] > color.shape[1] else 1
    out = a.copy()
    out[ys, xs, :3] = np.clip(np.rint(color[ys // fy, xs // fx]), 0, 255).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def prepare(src, flip_h, bg_remove, trim):
    img = src.convert("RGBA")
    if flip_h:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if bg_remove:
        img = remove_background(img)
    if trim:
        box = img.getchannel("A").getbbox()
        if box:
            img = img.crop(box)
    return img


def fit_resize(img, W, H, fit, pixel_art):
    rs = Image.NEAREST if pixel_art else Image.LANCZOS
    w, h = img.size
    if fit.startswith("Растянуть"):
        return img.resize((W, H), rs)
    if fit.startswith("Вписать"):
        k = min(W / w, H / h)
        nw, nh = max(1, round(w * k)), max(1, round(h * k))
        out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        out.paste(img.resize((nw, nh), rs), ((W - nw) // 2, (H - nh) // 2))
        return out
    k = max(W / w, H / h)
    nw, nh = max(W, round(w * k)), max(H, round(h * k))
    big = img.resize((nw, nh), rs)
    x, y = (nw - W) // 2, (nh - H) // 2
    return big.crop((x, y, x + W, y + H))


def process(src, W, H, fit, pixel_art):
    """Старая точка входа (без обработки фона) - оставлена для скриптов."""
    return fit_resize(src.convert("RGBA"), W, H, fit, pixel_art)


def alpha_kind(img):
    """'none' | 'binary' (только 0/255) | 'soft'."""
    lo, hi = img.getchannel("A").getextrema()
    if lo == 255:
        return "none"
    hist = img.getchannel("A").histogram()
    return "binary" if sum(hist[1:255]) == 0 else "soft"


def reflectivity(img):
    """Средний линейный цвет, как считает vtex."""
    px = img.convert("RGB").resize((64, 64), Image.BILINEAR)
    if np is not None:
        a = (np.asarray(px).astype(np.float64) / 255.0) ** 2.2
        return tuple(float(v) for v in a.reshape(-1, 3).mean(axis=0))
    data = list(px.getdata())
    return tuple(sum((p[i] / 255.0) ** 2.2 for p in data) / len(data) for i in range(3))


def build_vtf(img, fmt, mips, pixel_art, clamp, akind, nolod=False, aniso=False):
    flags = VTFFlags.EMPTY
    if akind == "soft" and fmt not in NO_ALPHA_FORMATS:
        flags |= VTFFlags.EIGHTBITALPHA
    elif akind == "binary" and fmt not in NO_ALPHA_FORMATS:
        flags |= VTFFlags.ONEBITALPHA
    if pixel_art:
        flags |= VTFFlags.POINT_SAMPLE
    if clamp:
        flags |= VTFFlags.CLAMP_S | VTFFlags.CLAMP_T
    if aniso:
        flags |= VTFFlags.ANISOTROPIC
    if nolod:
        flags |= VTFFlags.NO_LOD
    if not mips:
        flags |= VTFFlags.NO_MIP | VTFFlags.NO_LOD
    if fmt in NO_ALPHA_FORMATS:
        # Без этого энкодер DXT1 делает пиксели с альфой < 128 чёрными (1-битная альфа DXT1),
        # а цвет картинки должен остаться, раз альфу мы всё равно выбрасываем.
        img = img.copy()
        img.putalpha(255)
    vtf = VTF(img.width, img.height, (7, 2), ref=reflectivity(img), fmt=fmt, flags=flags)
    vtf.get(frame=0, mipmap=0).copy_from(img.tobytes())
    filt = FilterMode.NEAREST if pixel_art else FilterMode.BILINEAR
    vtf.compute_mipmaps(filt)  # заодно считает миниатюру low-res
    if not mips:
        vtf.mipmap_count = 1
    buf = io.BytesIO()
    vtf.save(buf)
    data = buf.getvalue()
    ver = (int.from_bytes(data[4:8], "little"), int.from_bytes(data[8:12], "little"))
    if ver != (7, 2):
        raise RuntimeError(tr("srctools записал версию {major}.{minor} вместо 7.2").format(major=ver[0], minor=ver[1]))
    return data


def decal_scale(units, tex_w):
    """Ширина декали в юнитах -> $decalscale (или None, если число кривое)."""
    try:
        u = float(str(units).replace(",", "."))
    except ValueError:
        return None
    if u <= 0:
        return None
    return round(u / tex_w, 6)


def vtf_width(path):
    """Ширина верхнего мипа из заголовка VTF (uint16 по смещению 16)."""
    with open(path, "rb") as f:
        head = f.read(20)
    if head[:4] != b"VTF\0":
        raise ValueError(tr("не VTF: {path}").format(path=path))
    return int.from_bytes(head[16:18], "little")


def set_vmt_decalscale(vmt_path, scale):
    """Переписать (или добавить) "$decalscale" в готовом VMT. -> True, если файл изменён."""
    with open(vmt_path, encoding="utf-8") as f:
        text = f.read()
    line = f'"$decalscale" "{scale:g}"'
    new, n = re.subn(r'"\$decalscale"\s+"?[0-9.eE+-]+"?', line, text, flags=re.I)
    if not n:
        new = re.sub(r"\{", "{\r\n\t" + line, text, count=1)
    if new == text:
        return False
    with open(vmt_path, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    return True


def make_vmt(shader, basetexture, alpha_mode, akind, nocull, decalscale=None, additive=False,
             nodecal=False, vertexcolor=False):
    decal = shader == DECAL
    lines = [f'"{"LightmappedGeneric" if decal else shader}"', "{", f'\t"$basetexture" "{basetexture}"']
    if decal:
        lines.append('\t"$decal" "1"')
        lines.append(f'\t"$decalscale" "{decalscale:g}"')
    if alpha_mode == "Авто":
        alpha_mode = {"none": "Нет", "binary": "$alphatest", "soft": "$translucent"}[akind]
    if alpha_mode == "$translucent":
        lines.append('\t"$translucent" "1"')
    elif alpha_mode == "$alphatest":
        lines.append('\t"$alphatest" "1"')
        lines.append('\t"$alphatestreference" "0.5"')
    if additive:
        lines.append('\t"$additive" "1"')
    if nocull:
        lines.append('\t"$nocull" "1"')
    if nodecal:
        lines.append('\t"$nodecal" "1"')
    if vertexcolor:
        lines.append('\t"$vertexcolor" "1"')
        lines.append('\t"$vertexalpha" "1"')
    lines.append("}")
    return "\r\n".join(lines) + "\r\n"


def valve_check(vtf_path, expected, has_alpha):
    """Декодирует файл через vtf2tga.exe и сравнивает с тем, что должно было получиться."""
    exe = find_vtf2tga()
    if not exe:
        return None, tr("vtf2tga.exe не найден (папка bin TF2), проверка пропущена")
    with tempfile.TemporaryDirectory() as td:
        tga = os.path.join(td, "out.tga")
        subprocess.run([exe, "-i", vtf_path, "-o", tga], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not os.path.isfile(tga):
            return 0.0, tr("vtf2tga НЕ смог прочитать файл")
        got = Image.open(tga).convert("RGBA")
        got.load()
    if got.size != expected.size:
        return 0.0, tr("vtf2tga прочитал размер {got}, ожидался {expected}").format(got=got.size, expected=expected.size)
    if np is None:
        return None, tr("numpy нет, PSNR не посчитан (файл читается)")
    a = np.asarray(got).astype(np.int32)
    b = np.asarray(expected).astype(np.int32)
    ch = 4 if has_alpha else 3
    mse = float(((a[..., :ch] - b[..., :ch]) ** 2).mean())
    db = 99.0 if mse == 0 else 10 * math.log10(255.0 ** 2 / mse)
    return db, f"{db:.1f} dB"


def decode_vtf(path):
    """VTF -> RGBA через декодер Valve (vtf2tga), не srctools (см. заголовок файла)."""
    with tempfile.TemporaryDirectory() as td:
        tga = os.path.join(td, "out.tga")
        exe = find_vtf2tga()
        if not exe:
            raise RuntimeError(tr("vtf2tga.exe не найден (TF2 bin)"))
        subprocess.run([exe, "-i", path, "-o", tga], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not os.path.isfile(tga):
            raise RuntimeError(tr("vtf2tga не прочитал {path}").format(path=path))
        img = Image.open(tga).convert("RGBA")
        img.load()
    return img


# Конвейер без Tk: s - снимок настроек {ключ: значение} (переменные Tk из рабочего потока трогать
# нельзя). Им пользуются и сохранение из окна, и пересборка выделенной в Hammer декали.
def render_image(src, s):
    img = prepare(src, s["flip_h"], s["bg_remove"], s["trim"])
    W, H = target_size(img.width, img.height, s["width"], s["height"])
    res = fit_resize(img, W, H, s["fit"], s["pixel_art"])
    return bleed_colors(res) if s["bleed"] else res


def pick_fmt(fmt_name, akind):
    f = FORMATS[fmt_name]
    if f is None:
        f = ImageFormats.DXT1 if akind == "none" else ImageFormats.DXT5
    return f


def encode_material(res, s, basetexture, shader, units=None):
    """-> (байты VTF, текст VMT, формат, akind)."""
    akind = alpha_kind(res)
    fmt = pick_fmt(s["fmt"], akind)
    data = build_vtf(res, fmt, s["mips"], s["pixel_art"], s["clamp"], akind, nolod=s["nolod"],
                     aniso=s["aniso"])
    sc = None
    if shader == DECAL:
        sc = decal_scale(units, res.width)
        if sc is None:
            raise ValueError(tr("ширина декали должна быть числом > 0"))
    vmt = make_vmt(shader, basetexture, s["alpha_mode"], "none" if fmt in NO_ALPHA_FORMATS else akind,
                   s["nocull"], sc, additive=s["additive"], nodecal=s["nodecal"],
                   vertexcolor=s["vertexcolor"])
    return data, vmt, fmt, akind


# Исходники картинок (до обработки) для пересборки декали с другими настройками прямо из Hammer:
# sources/<папка>/<имя>.png. Без исходника пересборка идёт из текущего VTF (с потерями DXT).
SOURCES = os.path.join(DATA, "sources")


def source_path(mat):
    return os.path.join(SOURCES, *mat.split("/")) + ".png"


def save_source_copy(img, mat):
    try:
        p = source_path(mat)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            img = img.convert("RGBA")
        img.save(p + ".tmp", "PNG", compress_level=1)
        os.replace(p + ".tmp", p)
    except Exception as e:
        hlog(f"source copy for {mat} failed: {e}")


# ---------------------------------------------------------------- имена

TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o",
                     "p", "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e",
                     "yu", "ya"]))
# слова, которые ничего не говорят о картинке (имена файлов по умолчанию, служебные части URL)
GENERIC = set("""image images img imgs photo photos picture pictures pic pics download downloads
unnamed untitled file files index default original orig large medium small thumb thumbs thumbnail
thumbnails preview full fullsize hqdefault maxresdefault sddefault mqdefault hq720 oip th
screenshot snapshot media attachment attachments upload uploads content asset assets raw source src
clip copy new final temp tmp jpg jpeg png webp gif bmp avif jfif scaled resized fit crop w h s
tbn and9gc encrypted gstatic""".split())
NAME_MAX = 40


def clean_name(s):
    s = s.strip().lower().replace(" ", "_").replace("-", "_")
    s = "".join(TRANSLIT.get(c, c) for c in s)
    return re.sub(r"[^a-z0-9_]", "", s)


def name_from_text(s):
    """Произвольная строка (имя файла, alt, запрос) -> осмысленное имя или ''."""
    if not s:
        return ""
    s = html.unescape(urllib.parse.unquote_plus(str(s))).lower()
    s = "".join(TRANSLIT.get(c, c) for c in s)
    words = []
    for t in re.split(r"[^a-z0-9]+", s):
        if not t or t in words:
            continue
        if re.fullmatch(r"\d+x\d+|\d+(px|w|h|p)?", t) and (len(t) >= 3 or "x" in t):
            continue  # размеры, id, даты
        if len(t) >= 12 and re.search(r"\d", t):
            continue  # хеши, base64
        if len(t) > 24:
            continue
        words.append(t)
    # Слово из 1-2 букв - не имя: Firefox отдаёт картинку файлом "i.webp", а материал "i"
    # в фильтре браузера Hammer совпадает с десятками других (лог 20:34, см. _hw_find_tile).
    meaningful = [t for t in words if t not in GENERIC and not t.isdigit() and len(t) >= 3]
    if not meaningful:
        return ""
    words = [t for t in words if t not in GENERIC]
    out = ""
    for t in words:
        if len(out) + len(t) + (1 if out else 0) > NAME_MAX:
            break
        out = f"{out}_{t}" if out else t
    return out or words[0][:NAME_MAX]


def url_parts(u):
    """URL -> (поисковый запрос[], имя файла, папки пути[] от ближней к дальней)."""
    try:
        parts = urllib.parse.urlsplit(u)
    except ValueError:
        return [], "", []
    if parts.scheme not in ("http", "https", "file"):
        return [], "", []
    q = urllib.parse.parse_qs(parts.query)
    query = [q[k][0] for k in ("q", "text", "query", "search", "k", "p", "tags") if k in q]
    segs = [urllib.parse.unquote(x) for x in parts.path.split("/") if x]
    base = os.path.splitext(segs[-1])[0] if segs else ""
    return query, base, list(reversed(segs[:-1]))


def names_from_url(u):
    """Кандидаты из адреса картинки: имя файла, потом запрос."""
    query, base, _ = url_parts(u)
    return [base] + query


def names_from_html(text):
    """Кандидаты из HTML (drag & drop или "HTML Format" буфера), по убыванию надёжности:
    alt/title картинки, поисковый запрос страницы (Google/Яндекс), имя файла картинки,
    путь страницы (у Reddit и блогов там заголовок поста)."""
    out = []
    m = re.search(r"<img\b[^>]*>", text, re.I)
    if m:
        for attr in ("alt", "title", "aria-label"):
            a = re.search(r"\s" + attr + r"""\s*=\s*["']([^"']*)["']""", m.group(0), re.I)
            if a:
                out.append(a.group(1))
    base = re.search(r"^SourceURL:(\S+)", text, re.M)
    page_q, page_base, page_dirs = url_parts(base.group(1)) if base else ([], "", [])
    out += page_q
    src = find_image_source(text) if m else None
    if src and src[0] == "url":
        out += names_from_url(src[1])
    return out + [page_base] + page_dirs


def pick_name(cands):
    for c in cands:
        n = name_from_text(c)
        if n:
            return n
    return ""


def clean_subdir(s):
    parts = [clean_name(p) for p in re.split(r"[\\/]+", s.strip())]
    return "/".join(p for p in parts if p)


def split_material_path(p):
    """...\\tf\\materials\\a\\b\\name.vtf -> (tf, "a/b", "name"); без materials -> (None, ..)."""
    parts = os.path.normpath(p).split(os.sep)
    idx = [i for i, x in enumerate(parts) if x.lower() == "materials"]
    if not idx or idx[-1] == 0:
        return None, None, None
    i = idx[-1]
    tf = os.sep.join(parts[:i])
    if tf.endswith(":"):
        tf += os.sep
    return tf, "/".join(parts[i + 1:-1]), os.path.splitext(parts[-1])[0]


# ---------------------------------------------------------------- источники картинки

URL_RE = re.compile(r"""(data:image/[^\s"'<>]+|https?://[^\s"'<>]+|file:///[^\s"'<>]+)""", re.I)
IMG_SRC_RE = re.compile(r"""<img\b[^>]*?\ssrc\s*=\s*["']([^"']+)["']""", re.I)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
MAX_DOWNLOAD = 64 * 1024 * 1024


def find_image_source(text):
    """Текст/HTML из буфера или drag & drop -> ('path', p) | ('url', u) | None.
    HTML идёт первым: у картинки внутри ссылки URL указывает на страницу, а src - на саму картинку."""
    text = text.strip().strip("\x00")
    if not text:
        return None
    if os.path.isfile(text.strip('"')):
        return "path", text.strip('"')
    m = IMG_SRC_RE.search(text)
    if m:
        src = html.unescape(m.group(1))
        base = re.search(r"^SourceURL:(\S+)", text, re.M)  # заголовок CF_HTML
        if base and not src.startswith(("http:", "https:", "data:")):
            src = urllib.parse.urljoin(base.group(1), src)
        return "url", src
    m = URL_RE.search(text)
    if m:
        u = m.group(1)
        if u.lower().startswith("file:"):
            return "path", urllib.request.url2pathname(urllib.parse.urlparse(u).path)
        return "url", u
    return None


def fetch_image(url):
    """URL / data: URI -> Image. Вызывается в фоновом потоке."""
    if url.lower().startswith("data:"):
        head, _, payload = url.partition(",")
        raw = base64.b64decode(payload) if ";base64" in head.lower() else urllib.parse.unquote_to_bytes(payload)
    else:
        parts = urllib.parse.urlsplit(url)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
            "Referer": f"{parts.scheme}://{parts.netloc}/"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(MAX_DOWNLOAD + 1)
            ctype = r.headers.get("Content-Type", "")
        if len(raw) > MAX_DOWNLOAD:
            raise ValueError(tr("файл больше 64 МБ"))
        if "svg" in ctype or raw.lstrip()[:5].lower() in (b"<svg ", b"<?xml"):
            raise ValueError(tr("это SVG (векторная картинка), сохрани её в PNG"))
        if ctype.startswith("text/html"):
            raise ValueError(tr("по ссылке веб-страница, а не картинка"))
    img = Image.open(io.BytesIO(raw))
    img.load()
    return img


def open_path(path):
    img = Image.open(path)
    img.load()
    return img


def file_stem(p):
    return os.path.splitext(os.path.basename(p))[0]


def clipboard_html():
    """Содержимое формата "HTML Format" буфера обмена ('' если нет). Браузер кладёт его вместе
    с картинкой при "Копировать изображение": там alt и SourceURL страницы."""
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.WinDLL("user32")
    k32 = ctypes.WinDLL("kernel32")
    u32.RegisterClipboardFormatW.restype = wintypes.UINT
    u32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    u32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.GetClipboardData.restype = wintypes.HANDLE
    u32.GetClipboardData.argtypes = [wintypes.UINT]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalSize.restype = ctypes.c_size_t
    k32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    fmt = u32.RegisterClipboardFormatW("HTML Format")
    if not fmt or not u32.IsClipboardFormatAvailable(fmt):
        return ""
    for _ in range(10):  # буфер может быть занят другим процессом
        if u32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        return ""
    try:
        h = u32.GetClipboardData(fmt)
        if not h:
            return ""
        p = k32.GlobalLock(h)
        if not p:
            return ""
        try:
            raw = ctypes.string_at(p, k32.GlobalSize(h))
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def set_clipboard_text(root, text):
    """Текст в буфер через WinAPI. root.clipboard_append() не годится: Tk сам отдаёт данные
    буфера, пока жив, и после закрытия программы скопированный путь пропадает из буфера."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32")
        k32 = ctypes.WinDLL("kernel32")
        k32.GlobalAlloc.restype = wintypes.HGLOBAL
        k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        u32.OpenClipboard.argtypes = [wintypes.HWND]
        u32.SetClipboardData.restype = wintypes.HANDLE
        u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        raw = (text + "\0").encode("utf-16-le")
        for _ in range(10):
            if u32.OpenClipboard(None):
                break
            time.sleep(0.02)
        else:
            raise OSError(tr("буфер обмена занят другой программой"))
        try:
            u32.EmptyClipboard()
            h = k32.GlobalAlloc(0x0002, len(raw))  # GMEM_MOVEABLE
            p = k32.GlobalLock(h)
            ctypes.memmove(p, raw, len(raw))
            k32.GlobalUnlock(h)
            u32.SetClipboardData(13, h)  # CF_UNICODETEXT; память теперь принадлежит системе
        finally:
            u32.CloseClipboard()
        return
    root.clipboard_clear()
    root.clipboard_append(text)


def grab_clipboard(root):
    """-> ('image', Image, [имена]) | ('url', url, [имена]) | ('error', текст, None)."""
    try:
        data = ImageGrab.grabclipboard()
    except Exception as e:
        return "error", tr("Не удалось прочитать буфер: {e}").format(e=e), None
    try:
        htm = clipboard_html()
    except Exception:
        htm = ""
    if isinstance(data, Image.Image):
        return "image", data, names_from_html(htm) if htm else []
    if isinstance(data, list):  # файлы, скопированные в Проводнике
        for p in data:
            try:
                return "image", open_path(p), [file_stem(p)]
            except Exception:
                continue
        return "error", tr("В буфере файлы, но ни один не открылся как картинка"), None
    for text, names in ((htm, names_from_html(htm) if htm else []), (None, [])):
        if text is None:
            try:
                text = root.clipboard_get()
            except Exception:
                text = ""
        src = find_image_source(text) if text else None
        if src and src[0] == "path":
            try:
                return "image", open_path(src[1]), [file_stem(src[1])]
            except Exception as e:
                return "error", tr("Файл не открылся: {e}").format(e=e), None
        if src and src[0] == "url":
            return "url", src[1], names
    return "error", tr("В буфере обмена нет картинки"), None


# ---------------------------------------------------------------- мост в Hammer++
# Выбрать текстуру в Hammer "снаружи" можно только через его браузер текстур:
# комбобокс "Current texture" на панели Textures - owner-draw без строк (5138 пунктов, в данных -
# указатели на объекты внутри Hammer), по имени его не выбрать. Браузер же перечитывает список
# материалов при открытии, так что только что сохранённый материал в нём уже есть.
# Порядок, проверенный на живом Hammer++ (2026-09-28):
#   1. BM_CLICK по "Browse..." (id 1010) на панели "Textures" -> открывается диалог "Textures";
#   2. WM_SETTEXT в Edit фильтра (комбо 1269 -> Edit 1001) + WM_COMMAND CBN_EDITCHANGE (5):
#      на один WM_SETTEXT Hammer не реагирует, на CBN_EDITUPDATE (6) тоже; CBN_EDITCHANGE
#      запускает его таймер набора, и список перефильтровывается через ~1.5 с (замерено);
#   3. клик по первой плитке окна текстур (id 106) -> Static 1267 показывает имя выбранной;
#   4. двойной клик по ней -> браузер закрывается, текстура становится текущей.
HAMMER_EXES = ("hammerplusplus.exe", "hammer.exe")


def _u32():
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                      wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.GetDlgItem.restype = wintypes.HWND
    u.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
    return ctypes, wintypes, u


def _hw_send(h, msg, wp=0, lp=0, timeout=3000):
    ctypes, wintypes, u = _u32()
    r = ctypes.c_size_t()
    ok = u.SendMessageTimeoutW(h, msg, wp, lp, 0x0002, timeout, ctypes.byref(r))  # SMTO_ABORTIFHUNG
    return r.value if ok else None


def _hw_text(h):
    ctypes, wintypes, u = _u32()
    b = ctypes.create_unicode_buffer(1024)
    _hw_send(h, 0x000D, 1024, ctypes.cast(b, ctypes.c_void_p).value)  # WM_GETTEXT
    return b.value


def _hw_class(h):
    ctypes, wintypes, u = _u32()
    b = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(h, b, 256)
    return b.value


def _hw_exe(pid):
    ctypes, wintypes, u = _u32()
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        b = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        k.QueryFullProcessImageNameW(h, 0, b, ctypes.byref(n))
        return os.path.basename(b.value).lower()
    finally:
        k.CloseHandle(h)


def _hw_windows(pid=None, parent=None):
    """Видимые окна: верхнего уровня процесса pid или прямые дети parent."""
    ctypes, wintypes, u = _u32()
    out = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(h, l):
        if not u.IsWindowVisible(h):
            return True
        if parent is not None:
            if u.GetParent(h) == parent:
                out.append(h)
        else:
            p = wintypes.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(p))
            if pid is None or p.value == pid:
                out.append(h)
        return True

    fn = proto(cb)
    if parent is not None:
        u.EnumChildWindows(parent, fn, 0)
    else:
        u.EnumWindows(fn, 0)
    return out


def hammer_find():
    """-> (pid, главное окно) первого запущенного Hammer++/Hammer, или (None, None)."""
    ctypes, wintypes, u = _u32()
    for h in _hw_windows():
        if u.GetParent(h):
            continue
        p = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(p))
        if _hw_exe(p.value) in HAMMER_EXES and _hw_text(h).lower().startswith("hammer"):
            return p.value, h
    return None, None


def _hw_find_desc(root, pred):
    for h in _hw_windows(parent=root):
        if pred(h):
            return h
        r = _hw_find_desc(h, pred)
        if r:
            return r
    return None


def _hw_browser(pid):
    """Открытый браузер текстур: диалог "Textures" с окном текстур (id 106)."""
    ctypes, wintypes, u = _u32()
    for h in _hw_windows(pid=pid):
        if _hw_class(h) == "#32770" and u.GetDlgItem(h, 106) and u.GetDlgItem(h, 1269):
            return h
    return None


def _norm_mat(s):
    return s.strip().replace("\\", "/").lower()


def _hw_open_browser(pid, main):
    ctypes, wintypes, u = _u32()
    bar = _hw_find_desc(main, lambda h: _hw_class(h) == "#32770" and _hw_text(h) == "Textures"
                        and u.GetDlgItem(h, 1010))
    if not bar:
        return None, tr("в Hammer не видно панели Textures (кнопки Browse...)")
    # Не BM_CLICK: он имитирует клик мышью и молча не срабатывает, если пользователь в этот момент
    # держит кнопку мыши (тащит следующую картинку) - лог 07:39: "browser did not open in 20s".
    # WM_COMMAND BN_CLICKED - то, что кнопка сама шлёт панели при нажатии; от мыши не зависит.
    # Постом: обработчик открывает модальный диалог и не вернётся, пока тот открыт.
    u.PostMessageW(bar, 0x0111, (0 << 16) | 1010, u.GetDlgItem(bar, 1010))
    # До 20 с: после появления нового материала Hammer при открытии браузера перечитывает список
    # (5000+ текстур), и первое открытие дольше 5 с - с таймаутом 5 с первый бросок и срывался
    # ("браузер текстур Hammer не открылся" в логе 07:33:02, второй бросок - за 2 с).
    t0 = time.time()
    while time.time() - t0 < 20:
        time.sleep(0.02)
        dlg = _hw_browser(pid)
        if dlg:
            hlog(f"browser opened in {time.time() - t0:.1f}s")
            time.sleep(0.2)
            return dlg, ""
    hlog("browser did not open in 20s")
    return None, tr("браузер текстур Hammer не открылся за 20 с")


def _hw_close_browser(pid, dlg):
    ctypes, wintypes, u = _u32()
    u.PostMessageW(dlg, 0x0111, 2, 0)  # WM_COMMAND IDCANCEL: текущая текстура не меняется
    for _ in range(100):  # 2 с
        time.sleep(0.02)
        if not _hw_browser(pid):
            return True
    return False


def _hw_pick_in_browser(dlg, material, wait=3.5, reload=False):
    """Фильтр = material, клик по первым плиткам, двойной клик по нужной.
    reload: перед применением нажать "Reload" браузера (перечитать выбранную текстуру с диска -
    для перезаписанного материала; что картинка при этом обновляется, НЕ проверено: Hammer не
    рисует плитки, пока его окно неактивно, и снять их со стороны не вышло).
    -> 'applied' | 'notfound' | 'stuck' | 'unknown'."""
    ctypes, wintypes, u = _u32()
    fcombo = u.GetDlgItem(dlg, 1269)
    fedit = u.GetDlgItem(fcombo, 1001) if fcombo else None
    sel = u.GetDlgItem(dlg, 1267)
    win = u.GetDlgItem(dlg, 106)
    if not (fedit and sel and win):
        return "unknown"
    buf = ctypes.create_unicode_buffer(material)
    _hw_send(fedit, 0x000C, 0, ctypes.cast(buf, ctypes.c_void_p).value)  # WM_SETTEXT
    _hw_send(dlg, 0x0111, (5 << 16) | 1269, fcombo)  # WM_COMMAND CBN_EDITCHANGE -> фильтр через ~1.5 с
    want = _norm_mat(material)
    t_set = time.time()
    t_end = t_set + wait
    while True:
        # Только что открытый браузер при инициализации может вернуть свой прежний фильтр
        # (старое материал со 2-й попытки находился именно так) - поставить заново.
        if _hw_text(fedit) != material:
            _hw_send(fedit, 0x000C, 0, ctypes.cast(buf, ctypes.c_void_p).value)
            _hw_send(dlg, 0x0111, (5 << 16) | 1269, fcombo)
            t_set = time.time()
        # пока фильтр не сработал (~1.5 с) - только первые плитки, потом - все видимые
        lp = _hw_find_tile(win, sel, want, full=time.time() - t_set > 1.6)
        if lp is not None:
            if reload and u.GetDlgItem(dlg, 1304):
                _hw_send(u.GetDlgItem(dlg, 1304), 0x00F5)  # BM_CLICK "Reload"
                time.sleep(0.3)
            u.PostMessageW(win, 0x0203, 1, lp)  # WM_LBUTTONDBLCLK -> применить и закрыть
            u.PostMessageW(win, 0x0202, 0, lp)
            for _ in range(100):  # 2 с
                time.sleep(0.02)
                if not u.IsWindow(dlg) or not u.IsWindowVisible(dlg):
                    return "applied"
            return "stuck"
        if time.time() > t_end:
            return "notfound"
        time.sleep(0.05)


def _hw_find_tile(win, sel, want, full=True):
    """Щёлкать по плиткам браузера текстур, пока в поле "выбрано" (sel) не окажется want.
    -> lParam точки плитки или None. Плитки идут НЕ по алфавиту, а в порядке загрузки
    материалов (снято вживую: фильтр "mgefurry/i" дал 31 плитку, "mgefurry/i" - 24-я), поэтому
    мало щёлкнуть по первой: короткое имя совпадает с чужими ("i" внутри "imusemouse...").
    full=False - только первые плитки (фильтр ещё не сработал, список - все 5000 текстур).
    Щелчок по пустому месту выделение не меняет - по этому видно, где список кончился."""
    ctypes, wintypes, u = _u32()

    def click(x, y):
        lp = (y << 16) | x
        _hw_send(win, 0x0201, 1, lp)  # WM_LBUTTONDOWN
        _hw_send(win, 0x0202, 0, lp)  # WM_LBUTTONUP
        return lp, _norm_mat(_hw_text(sel))

    if not full:
        for x, y in ((40, 40), (24, 24), (70, 70), (110, 110)):
            lp, t = click(x, y)
            if t == want:
                return lp
        return None
    r = wintypes.RECT()
    u.GetClientRect(win, ctypes.byref(r))
    seen = set()
    for page in range(6):
        new_on_page = False
        quiet = 0  # пикселей по вертикали без новых имён
        for y in range(8, max(9, r.bottom - 4), 24):
            new = False
            for x in range(8, max(9, r.right - 4), 16):
                lp, t = click(x, y)
                if t == want:
                    return lp
                if t not in seen:
                    seen.add(t)
                    new = new_on_page = True
            quiet = 0 if new else quiet + 24
            if quiet > 300:  # больше строки плиток самого крупного размера - список кончился
                return None
        if not new_on_page:
            return None
        _hw_send(win, 0x0115, 3, 0)  # WM_VSCROLL SB_PAGEDOWN - следующая страница плиток
        time.sleep(0.05)
    return None


def hammer_apply(material, attempts=4, new_file=False):
    """hammer_apply, но окна Hammer (браузер текстур) не мелькают на экране: см. _DialogHider.
    Если материал так и не нашёлся, браузер остаётся открытым - после выхода из with он снова
    виден пользователю (restore_all), как и задумано."""
    pid, main = hammer_find()
    if not main:
        return False, tr("Hammer++ не запущен")
    with hidden_dialogs(pid, main):
        return _hammer_apply_impl(material, attempts, new_file)


def _hammer_apply_impl(material, attempts=4, new_file=False):
    """Делает material (путь без materials/ и .vmt) текущей текстурой в запущенном Hammer.
    Возвращает (ok, сообщение). Вызывается из фонового потока: только Win32-сообщения.
    Новый материал Hammer++ находит при открытии браузера, но в ЭТОМ окне браузера его ещё нет -
    он появляется со следующего открытия (замерено: пауза 0.5/1/2 с между записью и открытием
    ничего не меняет, всегда 2-я попытка; "Reload" в браузере перечитывает только выбранную
    текстуру). Поэтому для только что созданного файла (new_file) браузер сначала открывается и
    сразу закрывается, а в общем случае - если материала нет, закрыть и открыть снова."""
    ctypes, wintypes, u = _u32()
    pid, main = hammer_find()
    if not main:
        return False, tr("Hammer++ не запущен")
    dlg = _hw_browser(pid)
    if dlg and new_file:  # уже открытый браузер нового файла не покажет
        _hw_close_browser(pid, dlg)
        dlg = None
    if new_file:
        primed, err = _hw_open_browser(pid, main)
        if not primed:
            return False, err
        _hw_close_browser(pid, primed)
    for attempt in range(attempts):
        if not dlg:
            dlg, err = _hw_open_browser(pid, main)
            if not dlg:
                return False, err
        res = _hw_pick_in_browser(dlg, material, reload=not new_file)
        if res == "applied":
            return True, tr("в Hammer выбрана текстура {material}").format(material=material) + (
                tr(" (попытка {n})").format(n=attempt + 1) if attempt else "")
        if res == "stuck":
            return False, tr("текстура выделена в браузере Hammer, но он не закрылся - кликни по ней дважды")
        if res == "unknown":
            return False, tr("не узнал браузер текстур этой версии Hammer")
        if attempt == attempts - 1:
            break
        _hw_close_browser(pid, dlg)
        dlg = None
        time.sleep(1.0)
    return False, (tr(("Hammer так и не увидел {material}. Проверь, что материал лежит в tf, из которого читает " "Hammer (браузер оставлен открытым с этим фильтром)")).format(material=material))


def enable_dpi_awareness():
    """Физические пиксели везде (курсор, окна Hammer, SendInput, геометрия Tk) - иначе при
    масштабе экрана 125% Windows пересчитывает координаты, и клик попадёт мимо точки."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def cursor_pos():
    ctypes, wintypes, u = _u32()
    p = wintypes.POINT()
    u.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def lbutton_down():
    ctypes, wintypes, u = _u32()
    return bool(u.GetAsyncKeyState(0x01) & 0x8000)


# Стандартные курсоры Windows (стрелка, I-образный, ожидание, изменение размера, рука...).
# У OLE-перетаскивания (файл из Проводника, картинка из браузера) курсор СВОЙ - из ресурсов
# ole32.dll (там 20 курсоров перетаскивания: "нельзя", копировать, переместить...). А когда в
# Discord / браузере просто держат кнопку (тащат окно за заголовок, выделяют текст, тянут
# вкладку) - курсор стандартный. По этому и отличаем.
# IDC_NO (32648) нарочно НЕ в списке: вдруг какая-то программа рисует "нельзя бросить" системным.
_STD_CURSOR_IDS = (32512, 32513, 32514, 32515, 32516, 32640, 32641, 32642, 32643, 32644, 32645,
                   32646, 32649, 32650, 32651, 32671, 32672)
_STD_CURSORS = None


def cursor_handle():
    ctypes, wintypes, u = _u32()

    class CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hCursor", ctypes.c_void_p), ("pt", wintypes.POINT)]
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(ci)
    if not u.GetCursorInfo(ctypes.byref(ci)) or not (ci.flags & 1):  # CURSOR_SHOWING
        return 0
    return ci.hCursor or 0


def ole_drag_cursor():
    """Идёт настоящее перетаскивание (OLE drag & drop), а не просто зажатая кнопка мыши."""
    global _STD_CURSORS
    ctypes, wintypes, u = _u32()
    if _STD_CURSORS is None:
        u.LoadCursorW.restype = ctypes.c_void_p
        u.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _STD_CURSORS = {u.LoadCursorW(None, i) for i in _STD_CURSOR_IDS} - {None, 0}
    h = cursor_handle()
    return bool(h) and h not in _STD_CURSORS


def root_pid_at(pt):
    """PID процесса, чьё окно верхнего уровня под точкой экрана (0 если нет)."""
    ctypes, wintypes, u = _u32()
    u.WindowFromPoint.restype = wintypes.HWND
    u.WindowFromPoint.argtypes = [wintypes.POINT]
    u.GetAncestor.restype = wintypes.HWND
    h = u.WindowFromPoint(wintypes.POINT(*pt))
    if not h:
        return 0
    root = u.GetAncestor(h, 2) or h  # GA_ROOT
    p = wintypes.DWORD()
    u.GetWindowThreadProcessId(root, ctypes.byref(p))
    return p.value


HAMMER_LOG = os.path.join(DATA, "clip2vtf_hammer.log")


def hlog(msg):
    """Журнал шагов моста в Hammer - первое, что читать, если картинка не появилась."""
    try:
        if os.path.exists(HAMMER_LOG) and os.path.getsize(HAMMER_LOG) > 1024 * 1024:
            os.replace(HAMMER_LOG, HAMMER_LOG + ".old")
        with open(HAMMER_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {msg}\n")
    except Exception:
        pass


def _hw_rect(h):
    ctypes, wintypes, u = _u32()
    r = wintypes.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def hammer_3d_view(main):
    """(hwnd, прямоугольник) 3D-вида активной карты или (None, None).
    Виды - окна Afx с дочерним "Title Window". У 3D-вида в классе стоит CS_OWNDC (0x20, нужен
    для 3D-отрисовки), у 2D-видов нет: "Afx:...:102b:..." против "Afx:...:1008:..." (проверено)."""
    ctypes, wintypes, u = _u32()
    mdi = _hw_find_desc(main, lambda h: _hw_class(h) == "MDIClient")
    if not mdi:
        return None, None
    active = _hw_send(mdi, 0x0229, 0, 0)  # WM_MDIGETACTIVE (lParam=0: без указателя)
    root = active or mdi
    found = []

    def walk(h):
        for c in _hw_windows(parent=h):
            if any(_hw_text(k) == "Title Window" for k in _hw_windows(parent=c)):
                found.append(c)
            walk(c)

    walk(root)
    for v in found:
        parts = _hw_class(v).split(":")
        try:
            if len(parts) >= 3 and int(parts[2], 16) & 0x20:
                return v, _hw_rect(v)
        except ValueError:
            continue
    return None, None


def hammer_views_rect(main):
    """Где показывать зону сброса: 3D-вид (в 2D-виде инструмент декалей ничего не ставит)."""
    v, rect = hammer_3d_view(main)
    return rect


def _hw_foreground(h):
    """Вывести окно на передний план. Windows разрешает SetForegroundWindow только
    процессу с последним вводом, поэтому поток на время цепляется к очереди активного окна."""
    ctypes, wintypes, u = _u32()
    k = ctypes.windll.kernel32
    msg = wintypes.MSG()
    u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # у рабочего потока должна быть очередь
    fg = u.GetForegroundWindow()
    fgt = u.GetWindowThreadProcessId(fg, None) if fg else 0
    me = k.GetCurrentThreadId()
    attached = bool(fgt and fgt != me and u.AttachThreadInput(me, fgt, True))
    try:
        if u.IsIconic(h):
            u.ShowWindow(h, 9)  # SW_RESTORE
        u.BringWindowToTop(h)
        u.SetForegroundWindow(h)
    finally:
        if attached:
            u.AttachThreadInput(me, fgt, False)


def _hw_raise(h):
    """Поднять чужое окно наверх стопки БЕЗ прав на активацию: TOPMOST и сразу NOTOPMOST
    (NOTOPMOST кладёт окно над всеми обычными). SetForegroundWindow Windows может запретить -
    так и было: активным осталось окно Проводника, а точку броска закрывало окно Claude."""
    ctypes, wintypes, u = _u32()
    u.SetWindowPos.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
    flags = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
    u.SetWindowPos(h, ctypes.c_void_p(-1), 0, 0, 0, 0, flags)  # HWND_TOPMOST
    u.SetWindowPos(h, ctypes.c_void_p(-2), 0, 0, 0, 0, flags)  # HWND_NOTOPMOST


def _real_click(pt):
    """Настоящий клик левой кнопкой в точке экрана (как рукой). Курсор потом возвращается."""
    ctypes, wintypes, u = _u32()

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]

    old = cursor_pos()
    # Координаты - в каждом событии (ABSOLUTE по всему виртуальному рабочему столу), одним
    # SendInput: движение мыши пользователя не может вклиниться между "поставить курсор" и
    # "нажать". Раньше SetCursorPos + отдельный клик - при движении мыши клик уходил мимо.
    vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)    # SM_X/YVIRTUALSCREEN
    vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)    # SM_CX/CYVIRTUALSCREEN
    ax = int(round((pt[0] - vx) * 65535 / max(1, vw - 1)))
    ay = int(round((pt[1] - vy) * 65535 / max(1, vh - 1)))
    base = 0x0001 | 0x8000 | 0x4000  # MOVE | ABSOLUTE | VIRTUALDESK
    arr = (INPUT * 3)(INPUT(0, MOUSEINPUT(ax, ay, 0, base, 0, 0)),
                      INPUT(0, MOUSEINPUT(ax, ay, 0, base | 0x0002, 0, 0)),   # LEFTDOWN
                      INPUT(0, MOUSEINPUT(ax, ay, 0, base | 0x0004, 0, 0)))   # LEFTUP
    global LAST_OWN_CLICK
    LAST_OWN_CLICK = time.time()  # drag_watch не должен счесть этот клик "пользователь кликнул в Hammer"
    sent = u.SendInput(3, arr, ctypes.sizeof(INPUT))
    time.sleep(0.05)
    hlog(f"click at {pt} (abs {ax},{ay}) sent={sent} cursor_after={cursor_pos()}")
    if abs(old[0] - pt[0]) + abs(old[1] - pt[1]) > 3:
        u.SetCursorPos(*old)


LAST_OWN_CLICK = 0.0        # время нашего последнего настоящего клика в Hammer
HAMMER_TOOLBAR_ID = 59401   # вертикальная панель инструментов Hammer++
ID_EDIT_UNDO = 57643        # стандартный MFC ID_EDIT_UNDO (и кнопка на панели 59408)
UNDO_CREATE_DECAL = "Undo Create Decal"  # текст пункта меню после постановки декали (прочитан вживую)


def hammer_tool_checked(main, tool_id, cache={}):
    """Нажата ли кнопка инструмента (TB_ISBUTTONCHECKED - без указателей, безопасно)."""
    ctypes, wintypes, u = _u32()
    tb = cache.get(main)
    if not tb or not u.IsWindow(tb):
        tb = _hw_find_desc(main, lambda h: _hw_class(h) == "ToolbarWindow32"
                           and u.GetDlgCtrlID(h) == HAMMER_TOOLBAR_ID)
        cache.clear()
        cache[main] = tb
    return bool(tb and _hw_send(tb, 0x040A, tool_id, 0, timeout=300))


def hammer_undo_text(main):
    """Текст пункта Edit -> Undo ("Undo Create Decal\\tCtrl+Z" -> "Undo Create Decal").
    MFC обновляет его только при открытии меню, поэтому сначала WM_INITMENUPOPUP (хэндл меню и
    индекс - без указателей). Сама строка читается GetMenuStringW: меню - объект user32,
    читается из любого процесса. Без WM_INITMENUPOPUP текст устаревший (проверено: был
    "Create Decal", после обновления - "Selection")."""
    ctypes, wintypes, u = _u32()
    u.GetMenu.restype = wintypes.HMENU
    u.GetSubMenu.restype = wintypes.HMENU
    u.GetMenuStringW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT]
    menu = u.GetMenu(main)
    if not menu:
        return ""
    for i in range(u.GetMenuItemCount(menu)):
        sub = u.GetSubMenu(menu, i)
        if not sub:
            continue
        b = ctypes.create_unicode_buffer(256)
        if u.GetMenuStringW(sub, ID_EDIT_UNDO, b, 256, 0):  # MF_BYCOMMAND
            _hw_send(main, 0x0117, sub, i)  # WM_INITMENUPOPUP
            u.GetMenuStringW(sub, ID_EDIT_UNDO, b, 256, 0)
            return b.value.split("\t")[0].strip()
    return ""


class RemoteMem:
    """Буфер ВНУТРИ процесса Hammer для сообщений common controls (SB_*, LVM_*), которые берут
    указатель: Windows не переносит их между процессами. Передать адрес из нашего процесса =
    Hammer пишет в случайное место своей памяти (так однажды и уронили его, SB_GETTEXTW)."""

    def __init__(self, pid, size=16384):
        import ctypes
        from ctypes import wintypes
        self.ct = ctypes
        k = self.k = ctypes.windll.kernel32
        k.OpenProcess.restype = wintypes.HANDLE
        k.VirtualAllocEx.restype = ctypes.c_void_p
        k.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
        k.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
        k.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                         ctypes.POINTER(ctypes.c_size_t)]
        k.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                        ctypes.POINTER(ctypes.c_size_t)]
        self.pid = pid
        self.hp = k.OpenProcess(0x0008 | 0x0010 | 0x0020, False, pid)  # VM_OPERATION|VM_READ|VM_WRITE
        self.addr = k.VirtualAllocEx(self.hp, None, size, 0x3000, 0x04) if self.hp else None
        if not self.addr:
            raise OSError(tr("нет доступа к памяти Hammer"))

    def write(self, data, off=0):
        got = self.ct.c_size_t()
        self.k.WriteProcessMemory(self.hp, self.addr + off, data, len(data), self.ct.byref(got))

    def read(self, n, off=0):
        buf = self.ct.create_string_buffer(n)
        got = self.ct.c_size_t()
        self.k.ReadProcessMemory(self.hp, self.addr + off, buf, n, self.ct.byref(got))
        return buf.raw[:got.value]

    def wstr(self, off=0, n=4096):
        return self.read(n, off).decode("utf-16-le", "replace").split("\0")[0]

    def close(self):
        if self.addr:
            self.k.VirtualFreeEx(self.hp, self.addr, 0, 0x8000)
            self.addr = None
        if self.hp:
            self.k.CloseHandle(self.hp)
            self.hp = None


def hammer_selection_text(main, rm, cache={}):
    """Вторая ячейка строки статуса Hammer - что выделено: "infodecal  [ID: 5371337] [dist: 79.5]"
    (прочитано вживую). SB_GETTEXTW - в буфер внутри Hammer (RemoteMem)."""
    ctypes, wintypes, u = _u32()
    sb = cache.get(main)
    if not sb or not u.IsWindow(sb):
        sb = _hw_find_desc(main, lambda h: _hw_class(h) == "msctls_statusbar32")
        cache.clear()
        cache[main] = sb
    if not sb:
        return ""
    rm.write(b"\0\0")
    if _hw_send(sb, 0x040D, 1, rm.addr, timeout=500) is None:  # SB_GETTEXTW(part 1)
        return ""
    return rm.wstr(0, 1024)


import struct as _struct  # noqa: E402


def _lv_rows(lv, rm):
    """Строки SysListView32 окна свойств: [(ключ, значение)]. LVITEMW и текст - в памяти Hammer."""
    rows = []
    n = _hw_send(lv, 0x1004, 0, 0) or 0  # LVM_GETITEMCOUNT
    for i in range(n):
        row = []
        for sub in (0, 1):
            # LVITEMW x64: mask, iItem, iSubItem, state, stateMask, [pad4], pszText, cchTextMax, ...
            rm.write(_struct.pack("<IiiII4xQi", 0x1, i, sub, 0, 0, rm.addr + 512, 1500) + b"\0" * 40)
            rm.write(b"\0\0", 512)
            _hw_send(lv, 0x1073, i, rm.addr)  # LVM_GETITEMTEXTW
            row.append(rm.wstr(512, 3000))
        rows.append(tuple(row))
    return rows


class _DialogHider:
    """Окна, которые Hammer создаёт, ПОКА наша автоматизация работает (браузер текстур, свойства,
    Transform), делаются невидимыми сразу при создании: layered с альфой 0 + сквозные для мыши.
    Сообщения они принимают как обычно - автоматизация не видит разницы, а пользователь не
    видит мелькания. Хук событий окон (SetWinEventHook, вне процесса Hammer, без инъекций)
    живёт в своём потоке с очередью сообщений. После работы стили возвращаются: окно свойств
    Hammer переиспользует, и иначе пользователь сам открыл бы его невидимым."""

    def __init__(self):
        self.pid = None
        self.main = None
        self.depth = 0    # вложенные with (выбор текстуры изнутри смены размера)
        self.hidden = {}  # hwnd -> исходный WS_EX
        self.lock = threading.Lock()
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="dialog-hider").start()
        self.ready.wait(2)

    def _run(self):
        ctypes, wintypes, u = _u32()
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        u.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD]
        u.GetAncestor.restype = wintypes.HWND
        proto = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
                                   wintypes.LONG, wintypes.DWORD, wintypes.DWORD)

        def on_event(hook, event, hwnd, id_obj, id_child, tid, t):
            try:
                if id_obj != 0 or not hwnd or self.pid is None or hwnd == self.main:
                    return
                p = wintypes.DWORD()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                if p.value != self.pid or u.GetAncestor(hwnd, 2) != hwnd:  # только верхнего уровня
                    return
                with self.lock:
                    if hwnd in self.hidden:
                        return
                    ex = u.GetWindowLongPtrW(hwnd, -20)
                    self.hidden[hwnd] = ex
                u.SetWindowLongPtrW(hwnd, -20, ctypes.c_void_p(ex | 0x00080000 | 0x20))  # LAYERED|TRANSPARENT
                u.SetLayeredWindowAttributes(hwnd, 0, 0, 0x2)  # альфа 0
            except Exception:
                pass

        self._cb = proto(on_event)
        # EVENT_OBJECT_CREATE..EVENT_OBJECT_SHOW, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        self._hook = u.SetWinEventHook(0x8000, 0x8002, None, self._cb, 0, 0, 0x0000 | 0x0002)
        self.ready.set()
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    def restore_all(self):
        ctypes, wintypes, u = _u32()
        with self.lock:
            items, self.hidden = list(self.hidden.items()), {}
        for hwnd, ex in items:
            if u.IsWindow(hwnd):
                u.SetWindowLongPtrW(hwnd, -20, ctypes.c_void_p(ex))
                if ex & 0x00080000:  # был layered сам по себе - вернуть непрозрачность
                    u.SetLayeredWindowAttributes(hwnd, 0, 255, 0x2)

    def __call__(self, pid, main):
        hider = self

        class _Ctx:
            def __enter__(self_):
                hider.depth += 1
                hider.pid, hider.main = pid, main
                return hider

            def __exit__(self_, *exc):
                hider.depth -= 1
                if hider.depth <= 0:
                    hider.depth = 0
                    hider.pid = None
                    time.sleep(0.05)  # последние события хука
                    hider.restore_all()
                return False

        return _Ctx()


DIALOG_HIDER = None  # создаётся в App.start_hammer_watch (нужна очередь сообщений в своём потоке)


def hidden_dialogs(pid, main):
    """with hidden_dialogs(pid, main): ... - окна Hammer, открытые внутри, не видны пользователю."""
    if DIALOG_HIDER is None:
        class _Nop:
            def __enter__(self):
                return None

            def __exit__(self, *e):
                return False
        return _Nop()
    return DIALOG_HIDER(pid, main)


def hammer_nudge_selection(pid, main, dx=1 / 64):
    """Сдвинуть выделение на dx по X и обратно (Tools -> Transform, режим Move) - чтобы Hammer
    пересчитал геометрию декали после смены материала: сам он этого не делает, пока декаль не
    сдвинуть (пользователь так и делал руками - в истории был шаг "Translation").
    1/64 точно представимо во float, поэтому x + 1/64 - 1/64 == x в пределах карты (без дрейфа).
    Окно Transform (команда 113) модальное: id 1194 Move, 114/115/116 X/Y/Z, IDOK."""
    ctypes, wintypes, u = _u32()

    def one(value):
        before = set(_hw_windows(pid=pid))
        u.PostMessageW(main, 0x0111, 113, 0)
        dlg = None
        t0 = time.time()
        while not dlg and time.time() - t0 < 3:
            time.sleep(0.02)
            dlg = next((h for h in _hw_windows(pid=pid) if h not in before and _hw_text(h) == "Transformation"), None)
        if not dlg:
            return False
        for rid in (1274, 1259, 1194, 1195):  # радиокнопки режима: включить только Move
            _hw_send(u.GetDlgItem(dlg, rid), 0x00F1, 1 if rid == 1194 else 0, 0)  # BM_SETCHECK
        _hw_send(dlg, 0x0111, 1194, u.GetDlgItem(dlg, 1194))  # BN_CLICKED Move - диалог узнает о режиме
        for eid, val in ((114, value), (115, "0"), (116, "0")):
            buf = ctypes.create_unicode_buffer(val)
            _hw_send(u.GetDlgItem(dlg, eid), 0x000C, 0, ctypes.cast(buf, ctypes.c_void_p).value)
        u.PostMessageW(dlg, 0x0111, 1, 0)  # IDOK
        t0 = time.time()
        while u.IsWindow(dlg) and u.IsWindowVisible(dlg) and time.time() - t0 < 3:
            time.sleep(0.02)
        return not (u.IsWindow(dlg) and u.IsWindowVisible(dlg))

    return one(repr(dx)) and one(repr(-dx))


def hammer_set_selected_decal_texture(make_new_mat, progress=lambda s: None,
                                      done=tr("размер выделенной декали изменён"), same=tr("размер и так такой")):
    """Выделенной в Hammer infodecal поставить другой материал (для нового размера).
    Путь проверен вживую: Edit -> Properties (32819) открывает немодальное "Object Properties";
    на странице "Class Info" в списке (1024) выбрать строку Texture (LVM_SETITEMSTATE) - в поле
    id 1 появляется полный путь материала; WM_SETTEXT туда + "Применить" (12321) меняет свойство,
    это один шаг истории "Change Properties" (Ctrl+Z откатывает; проверено с Redo/Undo).
    make_new_mat(текущий материал) -> (новый материал, новый ли файл[, приписка к сообщению]) -
    в рабочем потоке. -> (ok, сообщение)."""
    pid, main = hammer_find()
    if not main:
        return False, tr("Hammer++ не запущен")
    with hidden_dialogs(pid, main):  # окна свойств / браузера / Transform не мелькают
        ok, msg, changed = _set_decal_texture_impl(pid, main, make_new_mat, progress, done, same)
        if ok and changed:
            # Hammer не пересчитывает стоящую декаль после смены материала, пока её не сдвинуть
            progress(tr("обновляю декаль в Hammer..."))
            if not hammer_nudge_selection(pid, main):
                msg += tr(" (микросдвиг не удался - сдвинь декаль сам)")
    return ok, msg


def _set_decal_texture_impl(pid, main, make_new_mat, progress, done, same):
    ctypes, wintypes, u = _u32()
    rm = RemoteMem(pid)
    opened = False
    try:
        def find_props():
            return next((h for h in _hw_windows(pid=pid) if _hw_text(h).startswith("Object Properties")), None)

        dlg = find_props()
        if not dlg:
            u.PostMessageW(main, 0x0111, 32819, 0)  # Edit -> Properties (Alt+Enter)
            opened = True
            t0 = time.time()
            while not dlg and time.time() - t0 < 5:
                time.sleep(0.03)
                dlg = find_props()
            if not dlg:
                return False, tr("окно свойств Hammer не открылось"), False
        page = next((c for c in _hw_windows(parent=dlg) if _hw_text(c) == "Class Info"), None)
        lv = u.GetDlgItem(page, 1024) if page else None
        if not lv:
            return False, tr("не узнал окно свойств Hammer"), False
        rows = _lv_rows(lv, rm)
        idx = next((i for i, (key, _) in enumerate(rows) if key.lower() == "texture"), None)
        if idx is None:
            return False, tr("у выделенного объекта нет свойства Texture - это не infodecal"), False
        rm.write(_struct.pack("<IiiII", 0x8, idx, 0, 0x3, 0x3) + b"\0" * 60)  # LVIF_STATE: SELECTED|FOCUSED
        _hw_send(lv, 0x102B, idx, rm.addr)  # LVM_SETITEMSTATE
        ed = u.GetDlgItem(page, 1)
        cur = ""
        t0 = time.time()
        while not cur and time.time() - t0 < 2:
            time.sleep(0.02)
            cur = _hw_text(ed).strip()
        if not cur:
            return False, tr("не прочитал текущий материал декали"), False
        made = make_new_mat(cur)
        new_mat, note = made[0], (made[2] if len(made) > 2 else "")
        if _norm_mat(new_mat) == _norm_mat(cur):
            return True, same, False
        # Всегда: Hammer видит материал только если он был на диске при последнем открытии
        # браузера текстур; иначе подставит "missing". Файл мог быть создан раньше (тот же размер),
        # а браузер с тех пор не открывался - так что new_file тут не показатель.
        if True:
            progress(tr("Hammer перечитывает материалы..."))
            b, _ = _hw_open_browser(pid, main)
            if b:
                _hw_close_browser(pid, b)
        buf = ctypes.create_unicode_buffer(new_mat)
        _hw_send(ed, 0x000C, 0, ctypes.cast(buf, ctypes.c_void_p).value)  # WM_SETTEXT (+EN_CHANGE)
        _hw_send(page, 0x0111, (0x0300 << 16) | 1, ed)                    # EN_CHANGE - на всякий
        time.sleep(0.05)
        _hw_send(dlg, 0x0111, 12321, u.GetDlgItem(dlg, 12321))           # "Применить"
        time.sleep(0.15)
        now = dict(_lv_rows(lv, rm)).get(rows[idx][0], "")
        hlog(f"decal texture {cur!r} -> {new_mat!r}, list now {now!r}")
        if _norm_mat(now).split("/")[-1] != _norm_mat(new_mat).split("/")[-1]:
            return False, tr("Hammer не принял новый материал ({now})").format(now=now or tr("пусто")), False
        return True, f"{done} ({new_mat.split('/')[-1]}){note}", True
    finally:
        rm.close()
        if opened:
            d = next((h for h in _hw_windows(pid=pid) if _hw_text(h).startswith("Object Properties")), None)
            if d:
                u.PostMessageW(d, 0x0111, 2, 0)  # закрыть окно, которое открыли мы (Cancel после Apply)
                t0 = time.time()  # дождаться, пока спрячется - до возврата его нормального вида
                while u.IsWindow(d) and u.IsWindowVisible(d) and time.time() - t0 < 2:
                    time.sleep(0.02)


def resized_decal_material(cur_mat, units, roots):
    """VMT того же материала с другим $decalscale: "<база>_s<юниты>". Только для материалов,
    которые лежат файлом в одном из roots (декали clip2vtf). -> (материал, новый ли файл)."""
    base = re.sub(r"_s\d+(p\d+)?(_\d+)?$", "", cur_mat.strip().replace("\\", "/"))
    src = next((os.path.join(r, "materials", *cur_mat.split("/")) + ".vmt" for r in roots
                if r and os.path.isfile(os.path.join(r, "materials", *cur_mat.split("/")) + ".vmt")), None)
    if not src:
        raise FileNotFoundError(tr("materials/{cur_mat}.vmt не найден - размер меняется только у декалей из clip2vtf").format(cur_mat=cur_mat))
    with open(src, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r'"\$basetexture"\s+"([^"]+)"', text, re.I)
    if not m:
        raise ValueError(tr("в {cur_mat}.vmt нет $basetexture").format(cur_mat=cur_mat))
    tex = m.group(1).replace("\\", "/")
    vtf = next((os.path.join(r, "materials", *tex.split("/")) + ".vtf" for r in roots
                if r and os.path.isfile(os.path.join(r, "materials", *tex.split("/")) + ".vtf")), None)
    if not vtf:
        raise FileNotFoundError(tr("materials/{tex}.vtf не найден").format(tex=tex))
    width = vtf_width(vtf)
    new_mat = f"{base}_s{units:g}".replace(".", "p")
    new_file = False
    for r in roots:
        if not r or not os.path.isfile(os.path.join(r, "materials", *cur_mat.split("/")) + ".vmt"):
            continue
        dst = os.path.join(r, "materials", *new_mat.split("/")) + ".vmt"
        if not os.path.exists(dst):
            new_file = True
            with open(dst, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        set_vmt_decalscale(dst, round(units / width, 6))
    return new_mat, new_file


# Хвосты, которые clip2vtf добавляет к имени декали: _s<юниты> (размер), _x<хеш> (пересборка).
MAT_SUFFIX_RE = re.compile(r"(?:_x[0-9a-f]{6}|_s\d+(?:p\d+)?(?:_\d+)?)+$")


def rebuilt_decal_material(cur_mat, s, units, roots, progress=lambda t: None):
    """Та же картинка с другими настройками текстуры/материала: VTF + VMT "<база>_x<хеш>".
    Hammer не перечитывает уже загруженный материал, поэтому каждое сочетание настроек - свой
    файл; хеш от настроек, размера и исходника, так что возврат к прежним настройкам берёт уже
    готовый файл. Картинка - из sources/<база>.png (пишется при каждом сохранении), иначе из
    materialsrc/, иначе декодируется текущий VTF базового материала.
    roots[0] - клиентский tf (его читает Hammer), roots[1] - серверный.
    -> (материал, новый ли файл, приписка к сообщению)."""
    cur_mat = cur_mat.strip().replace("\\", "/")
    base = MAT_SUFFIX_RE.sub("", cur_mat)
    roots = [r for i, r in enumerate(roots) if r and os.path.isdir(r)
             and os.path.normcase(os.path.abspath(r)) not in
             [os.path.normcase(os.path.abspath(x)) for x in roots[:i] if x]]

    def mfile(root, mat, ext):
        return os.path.join(root, "materials", *mat.split("/")) + ext

    def find(mat, ext):
        return next((mfile(r, mat, ext) for r in roots if os.path.isfile(mfile(r, mat, ext))), None)

    vmt_mat = base if find(base, ".vmt") else cur_mat
    vmt_src = find(vmt_mat, ".vmt")
    if not vmt_src:
        raise FileNotFoundError(tr("materials/{cur_mat}.vmt не найден - пересобираются только декали из clip2vtf").format(cur_mat=cur_mat))
    note = ""
    src_file = source_path(base) if os.path.isfile(source_path(base)) else next(
        (p for r in roots for p in [os.path.join(r, "materialsrc", *base.split("/")) + ".png"]
         if os.path.isfile(p)), None)
    if not src_file:
        with open(vmt_src, encoding="utf-8") as f:
            m = re.search(r'"\$basetexture"\s+"([^"]+)"', f.read(), re.I)
        src_file = m and find(m.group(1).replace("\\", "/"), ".vtf")
        if not src_file:
            raise FileNotFoundError(tr("нет ни исходника, ни VTF для {base}").format(base=base))
        note = tr(" (исходника нет - собрано из текущей текстуры)")
    st = os.stat(src_file)
    key = json.dumps([{k: s[k] for k in REBUILD_KEYS}, float(units), src_file, st.st_mtime, st.st_size],
                     sort_keys=True, ensure_ascii=False)
    new_mat = f"{base}_x{hashlib.sha1(key.encode('utf-8')).hexdigest()[:6]}"
    # куда писать: клиентский tf всегда (Hammer), серверный - если там лежит сама декаль
    targets = [r for i, r in enumerate(roots) if i == 0 or os.path.isfile(mfile(r, vmt_mat, ".vmt"))]
    if all(os.path.isfile(mfile(r, new_mat, ".vmt")) and os.path.isfile(mfile(r, new_mat, ".vtf"))
           for r in targets):
        return new_mat, False, note  # это сочетание уже собиралось
    progress(tr("собираю текстуру с новыми настройками..."))
    if src_file.lower().endswith(".vtf"):
        src = decode_vtf(src_file)
    else:
        src = Image.open(src_file)
        src.load()
    res = render_image(src, s)
    data, vmt, fmt, akind = encode_material(res, s, new_mat, DECAL, units)
    for i, r in enumerate(targets):
        vp = mfile(r, new_mat, ".vtf")
        os.makedirs(os.path.dirname(vp), exist_ok=True)
        with open(vp, "wb") as f:
            f.write(data)
        with open(vp[:-4] + ".vmt", "w", encoding="utf-8", newline="") as f:
            f.write(vmt)
        if i > 0 and s.get("make_bz2"):  # .bz2 только серверной копии (FastDL)
            for p, raw in ((vp + ".bz2", data), (vp[:-4] + ".vmt.bz2", vmt.encode("utf-8"))):
                with open(p, "wb") as f:
                    f.write(bz2.compress(raw, 9))
    if s.get("verify"):
        db, msg = valve_check(mfile(targets[0], new_mat, ".vtf"), res,
                              fmt not in NO_ALPHA_FORMATS and akind != "none")
        if db is not None and db < MIN_PSNR:
            note += tr(" - vtf2tga {msg}, ПЛОХО, попробуй BGRA8888").format(msg=msg)
    return new_mat, True, note


def wait_mouse_released(limit=15.0):
    """Ждать, пока пользователь отпустит левую кнопку (тащит что-то ещё): наш настоящий клик
    смешался бы с его перетаскиванием."""
    t0 = time.time()
    while lbutton_down() and time.time() - t0 < limit:
        time.sleep(0.02)
    return not lbutton_down()


def hammer_place(material, new_file, tool_id, pt, progress=lambda s: None, face=None):
    """Перетащенная в Hammer картинка -> на стену: текстура текущая, инструмент оверлеев/декалей,
    клик в точку отпускания. Кликает сам Hammer-инструмент, так что это то же самое, что сделал
    бы маппер рукой (и Ctrl+Z его отменяет). Вызывается из фонового потока.
    Режим "текстура на браш" (TOOL_FACE) - свой порядок шагов, см. _hammer_texture_face."""
    ctypes, wintypes, u = _u32()
    t0 = time.time()
    hlog(f"--- drop {material} new={new_file} tool={tool_id} pt={pt} face={face}")
    if tool_id == TOOL_FACE:
        ok, msg = _hammer_texture_face(material, new_file, pt, face or {}, progress)
        hlog(f"done ok={ok} ({time.time() - t0:.1f}s) {msg}")
        return ok, msg
    progress(tr("выбираю текстуру в Hammer..."))
    ok, msg = hammer_apply(material, new_file=new_file)
    hlog(f"apply: ok={ok} {msg} ({time.time() - t0:.1f}s)")
    if not ok:
        return False, msg
    pid, main = hammer_find()
    if not main:
        return False, tr("Hammer++ закрылся")
    ok, msg2 = _hammer_click_at(pid, main, tool_id, pt, progress, msg)
    if not ok:
        hlog(f"done ok=False ({time.time() - t0:.1f}s)")
        return False, msg2
    note = _hammer_select_after_drop(pid, main, tool_id, pt, progress)
    hlog(f"done ok=True ({time.time() - t0:.1f}s)")
    if tool_id == TOOL_OVERLAY:
        return True, tr("оверлей {material} поставлен на стену - тяни за углы, чтобы изменить размер{note}").format(material=material, note=note)
    return True, tr("декаль {material} поставлена на стену{note}").format(material=material, note=note)


ID_TOOL_SELECTION = 32813  # "Selection" - первая кнопка панели инструментов (hammer_dll.dll)
# Окно "Face Edit Sheet" (инструмент Texture application), страница "Material" - id прочитаны
# с живого Hammer++: Apply 1015, Fit 1406, размер текстуры (Static) 1027.
FACE_APPLY, FACE_FIT, FACE_TEXSIZE = 1015, 1406, 1027
FACE_SCALE_X, FACE_SCALE_Y, FACE_ROTATION, FACE_LUXEL = 1009, 1150, 1023, 1389
FACE_JUSTIFY = {"По центру": 1405, "Влево": 1404, "Вправо": 1407, "Вверх": 1403, "Вниз": 1411}


def _face_number(ed):
    try:
        return float(_hw_text(ed).strip().replace(",", "."))
    except ValueError:
        return None


def _face_set(ed, value):
    ctypes, wintypes, u = _u32()
    buf = ctypes.create_unicode_buffer(f"{value:g}" if isinstance(value, float) else str(value))
    _hw_send(ed, 0x000C, 0, ctypes.cast(buf, ctypes.c_void_p).value)  # WM_SETTEXT


def _hammer_texture_face(material, new_file, pt, face, progress):
    """Картинка -> текстура грани под курсором. Порядок важен: клик инструментом Texture
    application в режиме "Lift+Select" выделяет грань И забирает её текстуру в текущую, поэтому
    свою текстуру выбираем ПОСЛЕ клика, потом поля (поворот, лайтмапа) + "Apply" (текущая
    текстура и поля -> выделенные грани), потом масштаб (Fit / по пропорциям / свой) и
    выравнивание. face = dict(mode, scale, align, rotate, luxel) - значения из BRUSH_*.
    Окна Hammer всё это время невидимы (hidden_dialogs), в конце - снова Selection (окно
    Face Edit при этом закрывается само). -> (ok, сообщение)."""
    ctypes, wintypes, u = _u32()
    pid, main = hammer_find()
    if not main:
        return False, tr("Hammer++ не запущен")

    def sheet_page():
        for h in _hw_windows(pid=pid):
            if _hw_text(h) == "Face Edit Sheet":
                for c in _hw_windows(parent=h):
                    if u.GetDlgItem(c, FACE_APPLY) and u.GetDlgItem(c, FACE_FIT):
                        return h, c
        return None, None

    with hidden_dialogs(pid, main):
        try:
            ok, msg = _hammer_click_at(pid, main, TOOL_FACE, pt, progress, "")
            if not ok:
                return False, msg.lstrip(". ")
            sheet = page = None
            t0 = time.time()
            while not page and time.time() - t0 < 3:
                time.sleep(0.05)
                sheet, page = sheet_page()
            if not page:
                return False, tr("не нашёл окно Face Edit в Hammer")
            time.sleep(0.2)  # клик дошёл: грань выделена
            hlog(f"face picked, texture size {_hw_text(u.GetDlgItem(page, FACE_TEXSIZE))!r}")
            progress(tr("выбираю текстуру в Hammer..."))
            ok, msg = hammer_apply(material, new_file=new_file)
            hlog(f"apply: ok={ok} {msg}")
            if not ok:
                return False, msg
            note = _face_layout(page, face)
            hlog(f"face applied, texture size now {_hw_text(u.GetDlgItem(page, FACE_TEXSIZE))!r}, "
                 f"undo={hammer_undo_text(main)!r}")
        finally:
            _hw_send(main, 0x0111, ID_TOOL_SELECTION, 0)  # как просил пользователь - снова Selection
            t0 = time.time()  # дождаться, пока Face Edit спрячется - до возврата его нормального вида
            while time.time() - t0 < 2:
                s, _ = sheet_page()
                if not s or not u.IsWindowVisible(s):
                    break
                time.sleep(0.02)
    return True, tr("текстура {material} положена на грань{note}").format(material=material, note=note)


def _face_layout(page, face):
    """Поля и кнопки Face Edit для выделенной грани (наша текстура уже текущая). -> приписка."""
    ctypes, wintypes, u = _u32()

    def ed(i):
        return u.GetDlgItem(page, i)

    def click(i):
        _hw_send(page, 0x0111, i, ed(i))  # BN_CLICKED
        time.sleep(0.15)

    mode = face.get("mode", BRUSH_FITS[0])
    if face.get("rotate", "0") in BRUSH_ROTATES:
        _face_set(ed(FACE_ROTATION), face.get("rotate", "0"))
    if face.get("luxel", BRUSH_LUXELS[0]) != BRUSH_LUXELS[0]:
        _face_set(ed(FACE_LUXEL), face["luxel"])
    click(FACE_APPLY)  # текстура + поворот + лайтмапа -> грань
    note = ""
    if mode == BRUSH_FITS[4]:  # масштаб Hammer: как есть, плиткой
        pass
    elif mode == BRUSH_FITS[3]:  # свой масштаб, знак (отражение) оставляем как был у грани
        try:
            v = abs(float(str(face.get("scale", "0.25")).replace(",", ".")))
        except ValueError:
            v = 0.25
        for i in (FACE_SCALE_X, FACE_SCALE_Y):
            old = _face_number(ed(i)) or 1.0
            _face_set(ed(i), float(v if old >= 0 else -v))
        click(FACE_APPLY)
    else:
        click(FACE_FIT)  # растянуть на грань: масштаб по X и Y - ровно грань
        sx, sy = _face_number(ed(FACE_SCALE_X)), _face_number(ed(FACE_SCALE_Y))
        hlog(f"face fit: scale {sx} x {sy}")
        note = tr(", растянута на всю грань")
        if mode != BRUSH_FITS[0] and sx and sy:
            # одинаковый масштаб по обеим осям: больший - закрыть грань (края обрезаются),
            # меньший - вписать целиком (остаток грани - повтор картинки)
            k = max(abs(sx), abs(sy)) if mode == BRUSH_FITS[1] else min(abs(sx), abs(sy))
            _face_set(ed(FACE_SCALE_X), float(k if sx > 0 else -k))
            _face_set(ed(FACE_SCALE_Y), float(k if sy > 0 else -k))
            click(FACE_APPLY)
            note = tr(", пропорции сохранены")
    align = face.get("align", BRUSH_ALIGNS[0])
    if mode != BRUSH_FITS[0] and align in FACE_JUSTIFY:  # после Fit выравнивать нечего
        click(FACE_JUSTIFY[align])
    return note


def _hammer_select_after_drop(pid, main, tool_id, pt, progress):
    """После постановки - обратно инструмент Selection (пользователь: "сразу на selection tool")
    и для декали - клик в ту же точку, чтобы она стала выделенной (появится панель размера).
    Проверка - по строке статуса: там должно стать "infodecal [ID: ...]". -> приписка к сообщению."""
    ctypes, wintypes, u = _u32()
    rm = RemoteMem(pid, 4096)
    try:
        time.sleep(0.1)
        before = hammer_selection_text(main, rm)
        _hw_send(main, 0x0111, ID_TOOL_SELECTION, 0)
        if tool_id != TOOL_DECAL:
            hlog(f"selection tool back; overlay, selection now {hammer_selection_text(main, rm)!r}")
            return ""
        progress(tr("выделяю декаль..."))
        # Второй щелчок в ту же точку раньше GetDoubleClickTime - это двойной щелчок, а он в
        # Hammer открывает свойства объекта. Ждём, пока окно двойного щелчка пройдёт.
        wait = u.GetDoubleClickTime() / 1000.0 + 0.08 - (time.time() - LAST_OWN_CLICK)
        if wait > 0:
            time.sleep(wait)
        if lbutton_down():
            wait_mouse_released()
        if root_pid_at(pt) != pid:
            hlog(f"select after drop: point covered, selection {before!r}")
            return tr(" (выделить не вышло: точку закрыло окно - кликни по декали)")
        _real_click(pt)
        sel = ""
        t0 = time.time()
        while time.time() - t0 < 1.5:
            sel = hammer_selection_text(main, rm)
            if sel.lower().startswith("infodecal") and sel != before:
                break
            time.sleep(0.05)
        hlog(f"select after drop: before={before!r} after={sel!r}")
        if sel.lower().startswith("infodecal"):
            return tr(", выделена")
        return tr(" (выделилась не декаль - кликни по ней)")
    finally:
        rm.close()


def _hammer_click_at(pid, main, tool_id, pt, progress, msg):
    """Инструмент + (при необходимости) поднять Hammer + настоящий клик в точку pt."""
    ctypes, wintypes, u = _u32()
    view, vrect = hammer_3d_view(main)
    hlog(f"3d view {view} rect={vrect} fg_before={_hw_text(u.GetForegroundWindow())[:60]!r}")
    if vrect and not (vrect[0] <= pt[0] < vrect[2] and vrect[1] <= pt[1] < vrect[3]):
        hlog("drop point is not in the 3D view")
        return False, msg + tr(". Отпусти картинку над 3D-видом - в 2D-видах декаль не ставится")
    _hw_send(main, 0x0111, tool_id, 0)  # WM_COMMAND: инструмент декалей / оверлеев
    time.sleep(0.15)
    if lbutton_down():
        progress(tr("отпусти кнопку мыши - поставлю декаль"))
        hlog("waiting for the user to release the mouse button")
        if not wait_mouse_released():
            return False, msg + tr(". Кнопка мыши так и не отпущена - кликни по стене сам")
    progress(tr("ставлю на стену..."))
    # Точку броска может закрывать другое окно: зона сброса была поверх всех, а под ней Hammer
    # частично закрыт (так и было - окном Claude). Поднимаем Hammer наверх: сначала честно
    # SetForegroundWindow, если Windows не дала - сменой z-порядка без активации.
    _hw_foreground(main)
    time.sleep(0.15)
    if root_pid_at(pt) != pid:
        _hw_raise(main)
        time.sleep(0.2)
    under = root_pid_at(pt)
    hlog(f"fg_after={_hw_text(u.GetForegroundWindow())[:60]!r} under_pt_pid={under} hammer={pid}")
    if under != pid:
        who = tr("окно clip2vtf") if under == os.getpid() else tr("другое окно")
        return False, msg + tr(". Точку закрывает {who} - инструмент включён, кликни по стене сам").format(who=who)
    _real_click(pt)  # настоящий клик сам активирует Hammer
    return True, msg


def clipboard_has_image():
    try:
        return isinstance(ImageGrab.grabclipboard(), (Image.Image, list))
    except Exception:
        return False


def checker(size, cell=8):
    w, h = size
    img = Image.new("RGB", size, (200, 200, 200))
    d = ImageDraw.Draw(img)
    for y in range(0, h, cell):
        for x in range((y // cell) % 2 * cell, w, cell * 2):
            d.rectangle([x, y, x + cell - 1, y + cell - 1], fill=(150, 150, 150))
    return img


# ---------------------------------------------------------------- окно

class _ItemRef:
    """Элемент холста с интерфейсом виджета (config/winfo_exists) - чтобы звёздочки и подсказки
    на холсте обслуживал тот же код, что раньше работал с ttk.Label."""

    def __init__(self, canvas, item):
        self.c, self.item = canvas, item

    def config(self, text=None, foreground=None, **_):
        kw = {}
        if text is not None:
            kw["text"] = text
        if foreground is not None:
            kw["fill"] = foreground
        if kw and self.winfo_exists():
            self.c.itemconfigure(self.item, **kw)

    configure = config

    def winfo_exists(self):
        try:
            return bool(self.c.winfo_exists() and self.c.find_withtag(self.item))
        except tk.TclError:
            return False


class CanvasPage(ttk.Frame):
    """Страница настроек, нарисованная на ОДНОМ tk.Canvas. Раньше каждая надпись, галочка и
    звёздочка была отдельным ttk-виджетом = отдельным окном Windows (235 на программу), и при
    каждом переключении вкладки / разворачивании окна они перерисовывались по одному - видно
    глазом. Холст хранит список нарисованного и сам буферизует вывод: перерисовка - один проход.
    Настоящими виджетами остались только выпадающие списки и поля ввода (create_window)."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app, px = app, app.px
        self.bg = ttk.Style().lookup("TFrame", "background") or "SystemButtonFace"
        self.c = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=self.bg)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.c.yview)
        self.c.configure(yscrollcommand=sb.set)
        self.c.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.c._scrollframe = self
        self.c.bind("<Configure>", self._on_resize)
        self.f_title = (UI_FONT, 9)
        self.f_desc = (UI_FONT, 8)
        self.f_star = ("Segoe UI Symbol", 11)
        self.x0 = px(6)                 # левое поле
        self.xc = self.x0 + px(22)      # колонка после звёздочки
        # колонка полей ввода - правее самой длинной подписи (на других языках они длиннее)
        lab_w = max(tkfont.Font(font=self.f_title).measure(line) for lab, _ in FIELD_DEFS.values()
                    for line in lab.split("\n"))
        self.xf = self.xc + max(px(118), lab_w + px(12))
        self.stretch = []               # (item, x) - окна, тянущиеся по ширине холста
        self.traces = []                # (var, id) - снять при очистке
        self.y = px(4)
        self._n = 0

    # -- служебное
    def _bottom(self, *items):
        return max((self.c.bbox(i) or (0, 0, 0, self.y))[3] for i in items)

    def _on_resize(self, e=None):
        w = self.c.winfo_width()
        for item, x in self.stretch:
            self.c.itemconfigure(item, width=max(self.app.px(60), w - x - self.app.px(10)))
        self.finish()

    def finish(self):
        self.c.configure(scrollregion=(0, 0, self.c.winfo_width(), self.y + self.app.px(10)))

    def clear(self):
        for var, tid in self.traces:
            try:
                var.trace_remove("write", tid)
            except Exception:
                pass
        self.traces.clear()
        for w in self.c.winfo_children():
            w.destroy()
        self.c.delete("all")
        self.stretch.clear()
        self.y = self.app.px(4)

    def wheel(self, delta):
        if self.y + self.app.px(10) > self.c.winfo_height():
            self.c.yview_scroll(int(-delta / 120) or (-1 if delta > 0 else 1), "units")

    def _hover(self, tag):
        self.c.tag_bind(tag, "<Enter>", lambda e: self.c.config(cursor="hand2"))
        self.c.tag_bind(tag, "<Leave>", lambda e: self.c.config(cursor=""))

    # -- содержимое
    def note(self, text, gap=4):
        px = self.app.px
        it = self.c.create_text(self.x0, self.y + px(gap), text=text, anchor="nw", width=px(340),
                                font=self.f_desc, fill="#6b6b6b")
        self.y = self._bottom(it)
        return _ItemRef(self.c, it)

    def star(self, key, y):
        it = self.c.create_text(self.x0, y, text="☆", anchor="nw", font=self.f_star, fill="#9a9a9a")
        self.c.tag_bind(it, "<Button-1>", lambda e, k=key: self.app.toggle_fav(k))
        self._hover(it)
        return _ItemRef(self.c, it)

    def check(self, key, text, desc, var, command):
        px = self.app.px
        self._n += 1
        tag = f"chk{self._n}"
        y = self.y + px(8)
        star = self.star(key, y - px(2))
        s = px(13)
        box = self.c.create_rectangle(self.xc, y + px(2), self.xc + s, y + px(2) + s, width=1,
                                      tags=(tag,))
        mark = self.c.create_line(self.xc + s * 0.22, y + px(2) + s * 0.52, self.xc + s * 0.42,
                                  y + px(2) + s * 0.74, self.xc + s * 0.80, y + px(2) + s * 0.28,
                                  width=max(2, px(2)), fill="white", tags=(tag,))
        title = self.c.create_text(self.xc + s + px(6), y, text=text, anchor="nw", font=self.f_title,
                                   fill="#1a1a1a", tags=(tag,))
        d = self.c.create_text(self.xc + px(20), self._bottom(title, box) + px(2), text=desc, anchor="nw",
                               width=px(300), font=self.f_desc, fill="#6b6b6b")

        def paint(*_):
            if not self.c.find_withtag(box):
                return
            on = bool(var.get())
            self.c.itemconfigure(box, fill="#1a6fd6" if on else "white",
                                 outline="#1a6fd6" if on else "#7a7a7a")
            self.c.itemconfigure(mark, state="normal" if on else "hidden")

        def toggle(e=None):
            var.set(not var.get())
            command()

        paint()
        self.traces.append((var, var.trace_add("write", paint)))  # копия в "Избранном" - та же переменная
        self.c.tag_bind(tag, "<Button-1>", toggle)
        self._hover(tag)
        self.y = self._bottom(d)
        return star

    def field(self, key, label, widget_factory):
        """Строка "звёздочка | подпись | виджет" - виджет настоящий (выпадающий список / поле)."""
        px = self.app.px
        y = self.y + px(3)
        star = self.star(key, y)
        lab = self.c.create_text(self.xc, y + px(3), text=label, anchor="nw", font=self.f_title, fill="#1a1a1a")
        w = widget_factory(self.c)
        item = self.c.create_window(self.xf, y, window=w, anchor="nw")
        self.stretch.append((item, self.xf))
        w.update_idletasks()
        self.y = max(self._bottom(lab), y + w.winfo_reqheight())
        return star, w

    def hint(self):
        it = self.c.create_text(self.xf, self.y + self.app.px(1), text="", anchor="nw", font=self.f_desc,
                                fill="#6b6b6b")
        self.y += self.app.px(16)
        return _ItemRef(self.c, it)

    def widget_row(self, widget_factory, indent=20, gap=2):
        px = self.app.px
        w = widget_factory(self.c)
        x = self.xc + px(indent)
        item = self.c.create_window(x, self.y + px(gap), window=w, anchor="nw")
        self.stretch.append((item, x))
        w.update_idletasks()
        self.y += px(gap) + w.winfo_reqheight()
        return w


class TabStack(ttk.Frame):
    """Вкладки-"предзагрузка": все страницы построены один раз, ОТОБРАЖЕНЫ и лежат стопкой в
    одной ячейке; переключение = tkraise (смена z-порядка). ttk.Notebook при каждом
    переключении делает unmap старой страницы и map новой, а на Windows каждая надпись/галочка
    Tk - отдельное системное окно (в программе их 235): 60-90 мс на КАЖДОЕ переключение, и
    второй раз не быстрее первого (замерено)."""

    def __init__(self, parent, on_change=None):
        super().__init__(parent)
        self.body = self  # страницы кладутся прямо сюда; кнопки вкладок рисует SidePanel
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.pages, self.titles = [], []
        self.cur = tk.IntVar(value=0)
        self.on_change = on_change
        self.on_select_visual = None  # SidePanel перекрашивает кнопки вкладок

    def add(self, page, text):
        page.grid(row=0, column=0, sticky="nsew")
        self.pages.append(page)
        self.titles.append(text)
        self._show(self.cur.get())

    def _show(self, i):
        # Скрытые страницы убираются из отображения (grid_remove): иначе при разворачивании окна
        # Tk перерисовывал все 4 страницы, включая невидимые (замерено: 5 холстов вместо 2).
        # Раньше map/unmap был дорогим из-за сотен окошек; теперь страница - один холст.
        for j, p in enumerate(self.pages):
            if j == i:
                p.grid()
            else:
                p.grid_remove()

    def select(self, i=None):
        if i is None:
            return self.cur.get()
        i = max(0, min(len(self.pages) - 1, int(i)))
        self.cur.set(i)
        self._show(i)
        if self.on_select_visual:
            self.on_select_visual(i)
        if self.on_change:
            self.on_change()


class SidePanel(tk.Canvas):
    """Вся правая панель на одном холсте: кнопки, подписи, вкладки, сводка - элементы рисунка.
    Настоящие окна - только поля ввода и страницы настроек (create_window). Почему: на Windows
    каждый виджет Tk - отдельное окно и при разворачивании перерисовывается отдельно
    (~1.6 мс на окно, ttk или классический tk - почти без разницы, замерено), поэтому окно
    "прорисовывалось" у пользователя на глазах. Холст рисует всё одним проходом."""

    BTN = dict(fill="#fdfdfd", outline="#c4c4c4")
    BTN_HOVER = dict(fill="#e5f1fb", outline="#0078d4")
    BTN_DOWN = dict(fill="#cce4f7", outline="#005a9e")
    TAB = dict(fill="", outline="")
    TAB_ON = dict(fill="#cce4f7", outline="#0078d4")

    def __init__(self, parent, app):
        bg = ttk.Style().lookup("TFrame", "background") or "SystemButtonFace"
        super().__init__(parent, highlightthickness=0, borderwidth=0, background=bg, width=app.px(400))
        self.app, self.px = app, app.px
        self.font = (UI_FONT, 9)
        self.buttons = {}   # имя -> dict(rect, text, cmd, style, selected)
        self.items = {}     # имя -> item (подписи, окна)
        self.layout_fn = None
        self.bind("<Configure>", lambda e: self.layout_fn and self.layout_fn(e.width, e.height))

    def button(self, name, text, cmd, tab=False):
        rect = self.create_rectangle(0, 0, 1, 1, width=1, tags=(name,))
        txt = self.create_text(0, 0, text=text, font=self.font, fill="#1a1a1a", tags=(name,))
        b = self.buttons[name] = dict(rect=rect, text=txt, cmd=cmd, tab=tab, selected=False, hover=False)
        self.tag_bind(name, "<Enter>", lambda e: self._paint(name, hover=True))
        self.tag_bind(name, "<Leave>", lambda e: self._paint(name, hover=False))
        self.tag_bind(name, "<ButtonPress-1>", lambda e: self._paint(name, down=True))
        self.tag_bind(name, "<ButtonRelease-1>", lambda e: self._release(name, e))
        self._paint(name)
        return b

    def _paint(self, name, hover=None, down=False):
        b = self.buttons[name]
        if hover is not None:
            b["hover"] = hover
            self.config(cursor="hand2" if hover else "")
        if down:
            st = self.BTN_DOWN
        elif b["tab"] and b["selected"]:
            st = self.TAB_ON
        elif b["hover"]:
            st = self.BTN_HOVER
        else:
            st = self.TAB if b["tab"] else self.BTN
        self.itemconfigure(b["rect"], **st)

    def _release(self, name, e):
        b = self.buttons[name]
        x1, y1, x2, y2 = self.coords(b["rect"])
        self._paint(name)
        if x1 <= e.x <= x2 and y1 <= e.y <= y2:
            b["cmd"]()

    def select_tab(self, name, on):
        self.buttons[name]["selected"] = on
        self._paint(name)

    def place_button(self, name, x, y, w, h):
        b = self.buttons[name]
        self.coords(b["rect"], x, y, x + w, y + h)
        self.coords(b["text"], x + w / 2, y + h / 2)
        # На других языках подпись бывает длиннее кнопки - тогда шрифт на пункт меньше.
        if not b["tab"]:
            self.itemconfigure(b["text"], font=self.font)
            if self.text_width(name) > w - self.px(8):
                self.itemconfigure(b["text"], font=(self.font[0], self.font[1] - 1))

    def text_width(self, name):
        x1, _, x2, _ = self.bbox(self.buttons[name]["text"])
        return x2 - x1


class StatusBar(tk.Canvas):
    """Строка статуса на холсте (config(text, foreground) / cget("text") - как у ttk.Label)."""

    def __init__(self, parent, px, text=""):
        bg = ttk.Style().lookup("TFrame", "background") or "SystemButtonFace"
        super().__init__(parent, highlightthickness=0, borderwidth=0, background=bg, height=px(22))
        self.line = self.create_line(0, 0, 10000, 0, fill="#c4c4c4")
        self.t = self.create_text(px(6), px(11), text=text, anchor="w", font=(UI_FONT, 9), fill="#1a1a1a")

    def config(self, cnf=None, **kw):
        if "text" in kw or "foreground" in kw:
            self.itemconfigure(self.t, text=kw.pop("text", self.itemcget(self.t, "text")),
                               fill=kw.pop("foreground", None) or "#1a1a1a")
        if kw or cnf:
            super().config(cnf, **kw)

    configure = config

    def cget(self, key):
        return self.itemcget(self.t, "text") if key == "text" else super().cget(key)


def install_fast_minimize(root):
    """"Свернуть" без потери нарисованного. Настоящее сворачивание сжимает окно до кнопки и
    уносит за экран - DWM выбрасывает его поверхность, и при разворачивании всё рисуется заново,
    а пока не дорисовано, видны чёрные прямоугольники (у Tk каждый виджет - своё окно).
    Здесь SC_MINIMIZE (кнопка "_", клик по активной кнопке на панели задач) перехватывается:
    окно ТОГО ЖЕ размера уносится за пределы всех мониторов, фокус отдаётся следующему окну.
    Поверхность жива, так что возврат (клик по кнопке на панели задач -> WM_ACTIVATE, или
    "Восстановить") - это просто перемещение: ноль перерисовки. Win+D и т.п. сворачивают как
    обычно. -> объект с .hwnd и .fake_minimized (для тестов)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    u.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.CallWindowProcW.restype = LRESULT
    u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    u.SetWindowLongPtrW.restype = ctypes.c_void_p
    u.GetWindow.restype = wintypes.HWND
    u.GetShellWindow.restype = wintypes.HWND
    u.SetWindowPos.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
    dwm = ctypes.windll.dwmapi
    hwnd = int(root.wm_frame(), 16)

    class State:
        pass

    st = State()
    st.hwnd, st.fake_minimized, st.pos = hwnd, False, None

    def next_window():
        """Следующее за нами обычное видимое окно (чтобы фокус ушёл туда, как при сворачивании)."""
        h = u.GetWindow(hwnd, 2)  # GW_HWNDNEXT
        me = os.getpid()
        while h:
            p = wintypes.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(p))
            cloaked = ctypes.c_int(0)
            dwm.DwmGetWindowAttribute(h, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
            ex = u.GetWindowLongW(h, -20)
            if (p.value != me and u.IsWindowVisible(h) and not u.IsIconic(h) and not cloaked.value
                    and not ex & 0x80 and u.GetWindowTextLengthW(h)):  # не WS_EX_TOOLWINDOW, с заголовком
                return h
            h = u.GetWindow(h, 2)
        return u.GetShellWindow()

    # Окно не двигается и не меняет размер: "свёрнутое" = полностью прозрачное (layered, альфа 0),
    # сквозное для мыши (WS_EX_TRANSPARENT) и в самом низу стопки. Первая версия уносила окно за
    # экран - Windows после возврата всё равно перерисовывала его целиком (16 Expose): область за
    # краем рабочего стола считается ненарисованной. Layered-стиль ставится сразу при запуске с
    # альфой 255, чтобы переключение альфы потом не пересоздавало поверхность.
    u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD]
    ex0 = u.GetWindowLongPtrW(hwnd, -20)
    u.SetWindowLongPtrW(hwnd, -20, ctypes.c_void_p(ex0 | 0x00080000))  # WS_EX_LAYERED
    u.SetLayeredWindowAttributes(hwnd, 0, 255, 0x2)  # LWA_ALPHA

    def set_ex(add=0, remove=0):
        ex = u.GetWindowLongPtrW(hwnd, -20)
        u.SetWindowLongPtrW(hwnd, -20, ctypes.c_void_p((ex | add) & ~remove))

    def fake_min():
        st.fake_minimized = True
        nxt = next_window()  # ДО ухода вниз стопки: потом под нами уже никого нет
        u.SetLayeredWindowAttributes(hwnd, 0, 0, 0x2)       # полностью прозрачное
        set_ex(add=0x20)                                   # WS_EX_TRANSPARENT: клики проходят насквозь
        u.SetWindowPos(hwnd, ctypes.c_void_p(1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_BOTTOM, NOACTIVATE
        if nxt:
            u.SetForegroundWindow(nxt)

    def fake_restore():
        st.fake_minimized = False
        set_ex(remove=0x20)
        u.SetWindowPos(hwnd, ctypes.c_void_p(0), 0, 0, 0, 0, 0x0001 | 0x0002)  # HWND_TOP
        u.SetLayeredWindowAttributes(hwnd, 0, 255, 0x2)

    def proc(h, msg, wp, lp):
        try:
            if msg == 0x0112:  # WM_SYSCOMMAND
                cmd = wp & 0xFFF0
                if cmd == 0xF020 and not u.IsIconic(hwnd):  # SC_MINIMIZE
                    fake_min()
                    return 0
                if cmd == 0xF120 and st.fake_minimized:  # SC_RESTORE (меню кнопки на панели задач)
                    fake_restore()
                    u.SetForegroundWindow(hwnd)
                    return 0
            elif msg == 0x0006 and (wp & 0xFFFF) and st.fake_minimized:  # WM_ACTIVATE: нас выбрали
                fake_restore()
        except Exception:
            pass
        return u.CallWindowProcW(st.old, h, msg, wp, lp)

    st.cb = WNDPROC(proc)  # держим ссылку, иначе колбэк соберёт GC
    st.old = u.SetWindowLongPtrW(hwnd, -4, ctypes.cast(st.cb, ctypes.c_void_p))  # GWLP_WNDPROC
    return st


def set_composited(hwnd):
    """WS_EX_COMPOSITED: Windows рисует окно со всеми дочерними окнами в память и показывает
    целиком - без видимой прорисовки 235 окошек по одному при разворачивании."""
    if sys.platform != "win32":
        return
    import ctypes
    u = ctypes.windll.user32
    u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    ex = u.GetWindowLongPtrW(ctypes.c_void_p(hwnd), -20)
    u.SetWindowLongPtrW(ctypes.c_void_p(hwnd), -20, ex | 0x02000000)


class HammerOptions:
    """Выпадающие настройки под панелью размера поверх Hammer: все галочки и списки вкладок
    Текстура и Материал, на тех же переменных, что в окне программы. Всё нарисовано на одном
    холсте, а списки открываются всплывающим tk.Menu - у панели одно окно Windows, и она
    показывается/прячется без поэлементной перерисовки (см. CanvasPage)."""
    BG, FG, DIM, ACC, ON = "#2b2b2b", "#f0f0f0", "#9a9a9a", "#ffd27f", "#1a6fd6"
    DEFAULT_HINT = tr("Наведи на настройку - здесь появится, что она делает. Изменение сразу " \
                   "пересобирает выделенную декаль и действует на следующие картинки.")

    def __init__(self, app, on_change):
        self.app, self.on_change = app, on_change
        px = app.px
        t = self.top = tk.Toplevel(app.root)
        t.withdraw()
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        c = self.c = tk.Canvas(t, bg=self.BG, highlightthickness=1, highlightbackground="#555555",
                               borderwidth=0)
        c.pack()
        self.f, self.fb, self.fs = (UI_FONT, 9), (UI_FONT, 9, "bold"), (UI_FONT, 8)
        colw, rowh, pad = px(310), px(21), px(10)
        y0 = pad
        bottom = y0
        fnt = tkfont.Font(font=self.f)
        for ci, (title, keys) in enumerate(HAMMER_OPT_COLUMNS):
            x = pad + ci * (colw + pad)
            labels = [FIELD_DEFS[k][0].replace("\n", " ") for k in keys if k not in CHECK_DEFS]
            self.val_dx = max([px(92)] + [fnt.measure(t) + px(10) for t in labels])
            y = y0
            c.create_text(x, y, anchor="nw", text=title, font=self.fb, fill=self.ACC)
            y += rowh
            for k in keys:
                (self._check if k in CHECK_DEFS else self._field)(k, x, y)
                y += rowh
            bottom = max(bottom, y)
        W = 2 * colw + 3 * pad
        c.create_line(pad, bottom + px(4), W - pad, bottom + px(4), fill="#444444")
        self.hint = c.create_text(pad, bottom + px(10), anchor="nw", width=W - 2 * pad,
                                  text=self.DEFAULT_HINT, font=self.fs, fill=self.DIM)
        c.config(width=W, height=bottom + px(10) + px(50))

    def _hover(self, tag, text):
        def enter(e):
            self.c.config(cursor="hand2")
            self.c.itemconfigure(self.hint, text=text, fill=self.FG)

        def leave(e):
            self.c.config(cursor="")
            self.c.itemconfigure(self.hint, text=self.DEFAULT_HINT, fill=self.DIM)
        self.c.tag_bind(tag, "<Enter>", enter)
        self.c.tag_bind(tag, "<Leave>", leave)

    def _check(self, key, x, y):
        c, px, var = self.c, self.app.px, self.app.v[key]
        tag = f"opt_{key}"
        s = px(13)
        y1 = y + px(2)
        box = c.create_rectangle(x, y1, x + s, y1 + s, width=1, tags=(tag,))
        mark = c.create_line(x + s * 0.22, y1 + s * 0.52, x + s * 0.42, y1 + s * 0.74, x + s * 0.80,
                             y1 + s * 0.28, width=max(2, px(2)), fill="white", tags=(tag,))
        c.create_text(x + s + px(6), y, anchor="nw", text=CHECK_DEFS[key][0], font=self.f,
                      fill=self.FG, tags=(tag,))

        def paint(*_):
            on = bool(var.get())
            c.itemconfigure(box, fill=self.ON if on else "#1e1e1e", outline=self.ON if on else "#8a8a8a")
            c.itemconfigure(mark, state="normal" if on else "hidden")

        def toggle(e):
            var.set(not var.get())
            self.on_change(key)
        paint()
        var.trace_add("write", paint)  # поменяли в окне программы - галочка здесь тоже
        c.tag_bind(tag, "<Button-1>", toggle)
        self._hover(tag, CHECK_DEFS[key][1])

    def _field(self, key, x, y):
        c, px, var = self.c, self.app.px, self.app.v[key]
        label, values = FIELD_DEFS[key]
        tag = f"opt_{key}"
        c.create_text(x, y, anchor="nw", text=label.replace("\n", " "), font=self.f, fill=self.FG,
                      tags=(tag,))
        val = c.create_text(x + self.val_dx, y, anchor="nw", font=self.f, fill=self.ACC, tags=(tag,))

        def paint(*_):
            c.itemconfigure(val, text=f"{tr(var.get())}  ▾")

        m = tk.Menu(c, tearoff=0)  # всплывающее меню - своё окно только пока открыто
        for v in values:
            m.add_radiobutton(label=tr(v), variable=var, value=v, command=lambda: self.on_change(key))

        def popup(e):
            try:
                m.tk_popup(e.x_root, e.y_root)
            finally:
                m.grab_release()
        paint()
        var.trace_add("write", paint)
        c.tag_bind(tag, "<Button-1>", popup)
        self._hover(tag, FIELD_HINTS.get(key, ""))

    def frame_id(self):
        return int(self.top.wm_frame(), 16)


class App:
    def __init__(self, root):
        self.root = root
        self.src = None
        self.names = []
        self.result = None
        self.photo = None
        self._prep_key = None
        self._prep = None
        s = dict(DEFAULTS)
        self.favorites = list(DEFAULT_FAVORITES)
        self.saved_tab = 0
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                loaded = json.load(f)
            s.update({k: v for k, v in loaded.items() if k in DEFAULTS})
            if isinstance(loaded.get("favorites"), list):
                self.favorites = [k for k in loaded["favorites"] if k in ALL_SETTINGS]
            self.saved_tab = loaded.get("tab", 0)
        except Exception:
            pass

        root.title(tr("clip2vtf - картинка в VTF + VMT"))
        # Процесс DPI-aware (см. enable_dpi_awareness), поэтому размеры в пикселях масштабируются
        # сами: шрифты Tk считает в пунктах, а вот ширины/переносы надо умножить.
        self.S = max(1.0, root.winfo_fpixels("1i") / 96.0)
        root.geometry(f"{self.px(1060)}x{self.px(760)}")
        root.minsize(self.px(900), self.px(600))
        self.v = {k: (tk.BooleanVar(value=v) if isinstance(v, bool) else tk.StringVar(value=v))
                  for k, v in s.items()}
        self.v["name"] = tk.StringVar()

        def game_dirs(*_):  # vtf2tga ищется в bin рядом с этими tf
            GAME_TFS[:] = [self.v["client_tf"].get().strip(), self.v["tf_root"].get().strip()]
        game_dirs()
        for k in ("client_tf", "tf_root"):
            self.v[k].trace_add("write", game_dirs)
        try:
            root.iconbitmap(default=os.path.join(RES, "clip2vtf.ico"))
        except tk.TclError:
            pass
        if UI_FONT != "Segoe UI":  # поля ввода и списки берут шрифт из именованных шрифтов Tk
            for fname in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkTooltipFont"):
                try:
                    tkfont.nametofont(fname).configure(family=UI_FONT)
                except tk.TclError:
                    pass
        style = ttk.Style()
        style.configure("Desc.TLabel", foreground="#6b6b6b", font=(UI_FONT, 8))
        style.configure("Hint.TLabel", foreground="#6b6b6b", font=(UI_FONT, 8))

        main = self.main_frame = ttk.Frame(root, padding=8)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(main, bg="#2b2b2b", highlightthickness=0, width=self.px(400), height=self.px(400))
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        # При сворачивании/разворачивании Configure приходит пачкой - рисуем один раз, когда утихнет
        self._prev_after = None
        self._prev_cache = None
        self.canvas.bind("<Configure>", self.on_canvas_configure)

        # -- правая панель: один холст (см. SidePanel), настоящие окна - только поля и страницы
        sp = self.side = SidePanel(main, self)
        sp.grid(row=0, column=1, sticky="nsew")
        sp.button("b_paste", tr("Вставить (Ctrl+V)"), self.paste)
        sp.button("b_open", tr("Открыть файл (Ctrl+O)"), self.open_file)
        sp.button("b_tf", "...", self.pick_tf)
        e_tf = ttk.Entry(sp, textvariable=self.v["tf_root"])
        e_sub = ttk.Entry(sp, textvariable=self.v["subdir"])
        e_name = ttk.Entry(sp, textvariable=self.v["name"])
        lab = {k: sp.create_text(0, 0, text=t, anchor="w", font=sp.font, fill="#1a1a1a")
               for k, t in (("tf", tr("Папка tf:")), ("sub", "materials/"), ("name", tr("Имя:")))}
        w_tf, w_sub, w_name = (sp.create_window(0, 0, window=e, anchor="nw") for e in (e_tf, e_sub, e_name))

        # -- вкладки. Каждая настройка строится build_setting(); на вкладке "Избранное" лежат
        # копии тех же элементов, привязанные к тем же переменным, поэтому значение общее.
        self.nb = nb = TabStack(sp)  # см. TabStack: страницы не пересоздаются при переключении
        w_nb = sp.create_window(0, 0, window=nb, anchor="nw")
        self.tabs = {}
        tab_keys = (("fav", tr("★ Избранное")), ("tex", tr("Текстура")), ("mat", tr("Материал")), ("save", tr("Сохранение")), ("hammer", "Hammer"))
        for i, (key, title) in enumerate(tab_keys):
            page = CanvasPage(nb.body, self)
            nb.add(page, text=title)
            self.tabs[key] = page
            sp.button(f"tab{i}", title, lambda i=i: nb.select(i), tab=True)
        nb.on_select_visual = lambda i: [sp.select_tab(f"tab{j}", j == i) for j in range(len(tab_keys))]
        nb.on_select_visual(0)

        # -- низ: сводка и кнопки
        info_item = sp.create_text(0, 0, text="", anchor="nw", font=sp.font, fill="#1a1a1a",
                                   width=self.px(380))
        self.info = _ItemRef(sp, info_item)
        sp.button("b_save", tr("Сохранить VTF + VMT  (Ctrl+S)"), self.save)
        sp.button("b_copy", tr("Копировать путь"), self.copy_path)
        sp.button("b_folder", tr("Открыть папку"), self.open_folder)
        sp.button("b_hammer", tr("В Hammer (Ctrl+H)"), self.send_to_hammer)

        def layout(W, H):
            px = self.px
            g, bh, rh = px(4), px(30), px(28)
            y = 0
            half = (W - g) / 2
            sp.place_button("b_paste", 0, y, half, bh)
            sp.place_button("b_open", half + g, y, W - half - g, bh)
            y += bh + px(8)
            lw = max(px(78), max(sp.bbox(i)[2] - sp.bbox(i)[0] for i in lab.values()) + px(8))
            dots = px(30)
            for key, win, extra in (("tf", w_tf, dots + g), ("sub", w_sub, 0), ("name", w_name, 0)):
                sp.coords(lab[key], 0, y + rh / 2)
                sp.coords(win, lw, y + px(2))
                sp.itemconfigure(win, width=max(px(40), W - lw - extra), height=rh - px(4))
                if key == "tf":
                    sp.place_button("b_tf", W - dots, y + px(1), dots, rh - px(2))
                y += rh + px(2)
            y += px(6)
            # вкладки в одну строку: сначала ужимаются отступы, потом (длинные языки) шрифт
            tabs = [f"tab{i}" for i in range(len(tab_keys))]
            for font in (sp.font, (sp.font[0], sp.font[1] - 1)):
                for n in tabs:
                    sp.itemconfigure(sp.buttons[n]["text"], font=font)
                widths = [sp.text_width(n) for n in tabs]
                pad = next((p for p in (px(20), px(14), px(8)) if sum(widths) + len(tabs) * (p + px(2)) <= W),
                           None)
                if pad is not None:
                    break
            pad = px(6) if pad is None else pad
            x = 0
            for n, tw in zip(tabs, widths):
                sp.place_button(n, x, y, tw + pad, bh - px(2))
                x += tw + pad + px(2)
            y += bh
            bottom = bh * 2 + px(4) + px(76)  # сводка ~4 строки + 2 ряда кнопок
            sp.coords(w_nb, 0, y + px(2))
            sp.itemconfigure(w_nb, width=W, height=max(px(80), H - y - bottom - px(6)))
            yb = H - bottom + px(4)
            sp.coords(info_item, 0, yb)
            sp.itemconfigure(info_item, width=W)
            yb = H - bh * 2 - px(4)
            sp.place_button("b_save", 0, yb, W, bh)
            yb += bh + px(4)
            third = (W - 2 * g) / 3
            for j, n in enumerate(("b_copy", "b_folder", "b_hammer")):
                sp.place_button(n, j * (third + g), yb, third, bh)

        sp.layout_fn = layout
        self.stars = {}  # ключ -> звёздочки этой настройки на всех вкладках
        self.decal_entries = []
        self.shader_hints = []
        for tab, items in TAB_LAYOUT.items():
            for item in items:
                if isinstance(item, tuple):  # ("note", текст)
                    self.tabs[tab].note(item[1])
                else:
                    self.build_setting(self.tabs[tab], item)
            self.tabs[tab].finish()
        self.rebuild_fav()
        try:
            nb.select(int(self.saved_tab))
        except Exception:
            pass
        nb.on_change = self.save_settings

        self.status = StatusBar(root, self.px, tr("Скопируй картинку и нажми Ctrl+V или перетащи её в окно"))
        self.status.pack(fill="x", side="bottom", before=main)
        if not self.v["tf_root"].get().strip():
            self.set_status(tr("TF2 не найден через Steam - укажи папку tf кнопкой \"...\" справа вверху"),
                            error=True)

        self.build_menu()
        self.jobs = queue.Queue()
        self.busy = False
        self.hammer_busy = False
        self.last_save_text = ""
        self.last_saved = None       # (материал, новый ли для клиентского tf) после удачного save()
        self.pending_drop = None     # точка экрана, куда отпустили картинку над Hammer
        self._was_down = False
        self._press_outside = False  # кнопку нажали вне Hammer и вне нас = тащат что-то извне
        self._hpid = self._hmain = None
        self._drop_shown = False
        self._hide_id = None
        self.poll_jobs()
        self.setup_dnd()
        root.bind_all("<Control-KeyPress>", self.on_ctrl_key)
        root.bind_all("<F1>", lambda e: self.show_help())
        root.bind_all("<MouseWheel>", self.on_wheel)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.apply_topmost()
        self.refresh()
        root.update_idletasks()
        self._fastmin = install_fast_minimize(root)  # сворачивание без перерисовки

    def px(self, n):
        return int(round(n * getattr(self, "S", 1.0)))

    # -- настройки и избранное
    def build_setting(self, page, key):
        """Одна настройка на странице-холсте (CanvasPage)."""
        if key in CHECK_DEFS:
            text, desc = CHECK_DEFS[key]
            star = page.check(key, text, desc, self.v[key], self.on_option)
            if key == "mirror_client":
                def client_row(master):
                    fr = ttk.Frame(master)
                    ttk.Label(fr, text=tr("tf клиента:")).pack(side="left")
                    ttk.Entry(fr, textvariable=self.v["client_tf"]).pack(side="left", fill="x", expand=True,
                                                                         padx=(4, 0))
                    ttk.Button(fr, text="...", width=3, command=self.pick_client).pack(side="left")
                    return fr
                page.widget_row(client_row)
        else:
            label, values = FIELD_DEFS[key]

            def make(master):
                if values is None:
                    # Поле ширины декали активно только с шейдером "Декаль": иначе его легко
                    # заполнить, не переключив шейдер, и получить VertexLitGeneric без $decalscale.
                    w = ttk.Entry(master, textvariable=self.v[key])
                    w.bind("<KeyRelease>", lambda e: self.refresh())
                    if key == "decal_units":
                        self.decal_entries.append(w)
                    else:
                        w.bind("<FocusOut>", lambda e: self.save_settings())
                else:
                    w = self.value_combo(master, key, values)
                return w

            star, _ = page.field(key, label, make)  # "\n" в подписи - перенос (колонка узкая)
            if key == "shader":
                self.shader_hints.append(page.hint())
        self.stars.setdefault(key, []).append(star)
        self.paint_star(star, key)

    def value_combo(self, master, key, values):
        """Выпадающий список: в настройке - внутреннее значение (по-русски, как в сохранённых
        настройках), на экране - перевод. Отдельная переменная для показа, синхронная с настройкой."""
        var = self.v[key]
        shown = tk.StringVar(value=tr(var.get()))
        w = ttk.Combobox(master, textvariable=shown, values=[tr(v) for v in values], state="readonly",
                         width=30)

        def picked(e=None):
            i = w.current()
            if 0 <= i < len(values):
                var.set(values[i])
            self.on_option()

        def follow(*_):  # настройку поменяли в другом месте (Избранное, панель над Hammer)
            if w.winfo_exists() and shown.get() != tr(var.get()):
                shown.set(tr(var.get()))
        w.bind("<<ComboboxSelected>>", picked)
        tid = var.trace_add("write", follow)
        w.bind("<Destroy>", lambda e: e.widget is w and var.trace_remove("write", tid))
        return w

    def paint_star(self, star, key):
        fav = key in self.favorites
        star.config(text="★" if fav else "☆", foreground="#e8a317" if fav else "#9a9a9a")

    def toggle_fav(self, key):
        if key in self.favorites:
            self.favorites.remove(key)
        else:
            self.favorites.append(key)
        for s in self.stars.get(key, []):
            if s.winfo_exists():
                self.paint_star(s, key)
        # не сносим виджет прямо в обработчике его же клика
        self.root.after_idle(self.rebuild_fav)
        self.save_settings()

    def rebuild_fav(self):
        fav = self.tabs["fav"]
        fav.clear()
        self.stars = {k: [s for s in v if s.winfo_exists()] for k, v in self.stars.items()}
        self.decal_entries = [w for w in self.decal_entries if w.winfo_exists()]
        self.shader_hints = [w for w in self.shader_hints if w.winfo_exists()]
        fav.note(tr("Нажми ☆ рядом с любой настройкой на других вкладках - она появится здесь. "
                 "★ убирает её отсюда. Значения общие: поменял здесь - поменялось и там."), gap=0)
        keys = [k for k in ALL_SETTINGS if k in self.favorites]
        for k in keys:
            self.build_setting(fav, k)
        if not keys:
            fav.note(tr("Пока пусто."), gap=12)
        fav.finish()
        self.refresh_controls()

    def on_wheel(self, e):
        w = self.root.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            sf = getattr(w, "_scrollframe", None)
            if sf is not None:
                sf.wheel(e.delta)
                return "break"
            w = w.master
        return None

    def build_menu(self):
        bar = tk.Menu(self.root)
        m = tk.Menu(bar, tearoff=0)
        m.add_command(label=tr("Вставить из буфера"), accelerator="Ctrl+V", command=self.paste)
        m.add_command(label=tr("Открыть файл..."), accelerator="Ctrl+O", command=self.open_file)
        m.add_separator()
        m.add_command(label=tr("Сохранить"), accelerator="Ctrl+S", command=self.save)
        m.add_command(label=tr("Сохранить как..."), accelerator="Ctrl+Shift+S", command=self.save_as)
        m.add_separator()
        m.add_command(label=tr("Открыть папку"), accelerator="Ctrl+E", command=self.open_folder)
        m.add_command(label=tr("Отправить в Hammer++"), accelerator="Ctrl+H", command=self.send_to_hammer)
        m.add_separator()
        m.add_command(label=tr("Выход"), accelerator="Ctrl+Q", command=self.close)
        bar.add_cascade(label=tr("Файл"), menu=m)
        m = tk.Menu(bar, tearoff=0)
        m.add_command(label=tr("Копировать путь материала"), accelerator="Ctrl+Shift+C", command=self.copy_path)
        bar.add_cascade(label=tr("Правка"), menu=m)
        # как ставить брошенную на Hammer картинку - меняется часто, поэтому и в меню
        m = tk.Menu(bar, tearoff=0)
        for mode in HAMMER_MODES:
            m.add_radiobutton(label=tr(mode), variable=self.v["hammer_mode"], value=mode,
                              command=self.on_option)
        bar.add_cascade(label=tr("Режим Hammer"), menu=m)
        m = tk.Menu(bar, tearoff=0)
        m.add_command(label=tr("Горячие клавиши"), accelerator="F1", command=self.show_help)
        bar.add_cascade(label=tr("Справка"), menu=m)
        # Язык: названия - на самих языках, а у меню подпись и на английском - чтобы найти его,
        # даже если текущий язык не читаешь.
        m = tk.Menu(bar, tearoff=0)
        for code, name in LANGUAGES:
            m.add_radiobutton(label=name, variable=self.v["lang"], value=code,
                              command=lambda c=code: self.set_language(c))
        title = tr("Язык")
        bar.add_cascade(label=title if LANG == "en" else f"{title} / Language", menu=m)
        self.root.config(menu=bar)

    def set_language(self, code):
        """Язык меняется перезапуском: все надписи собираются при запуске. Загруженная картинка
        переезжает в новый экземпляр через временный файл (как файл из командной строки)."""
        if code == LANG:
            return
        self.v["lang"].set(code)  # пункт меню ставит его сам, но вызов может быть и не из меню
        self.save_settings()
        args = []
        if self.src is not None:
            try:
                d = tempfile.mkdtemp(prefix="clip2vtf_lang_")
                name = clean_name(self.v["name"].get()) or "image"
                p = os.path.join(d, name + ".png")
                self.src.save(p)
                args.append(p)
            except Exception:
                pass
        cmd = [sys.executable] + ([] if FROZEN else [os.path.abspath(__file__)]) + args
        try:
            subprocess.Popen(cmd, close_fds=True)
        except OSError as e:
            self.set_status(tr("Не удалось перезапустить: {e}").format(e=e), error=True)
            return
        self.close()

    # -- drag & drop
    def setup_dnd(self):
        if tkinterdnd2 is None:
            self.dnd_ok = False
            return
        try:
            TkinterDnD._require(self.root)
            # Картинку, перетащенную из браузера как виртуальный файл (FileGroupDescriptorW), tkdnd
            # сохраняет сюда.
            tmp = os.path.join(tempfile.gettempdir(), "clip2vtf_drop")
            os.makedirs(tmp, exist_ok=True)
            # Методы tkinterdnd2 есть только у обычных виджетов (BaseWidget), у tk.Tk их нет,
            # поэтому цель - главная рамка: она покрывает превью и всю панель настроек.
            target = self.main_frame
            target.set_dropfile_tempdir(tmp)
            # Порядок = приоритет: tkdnd берёт первый тип из этого списка, который есть у источника.
            target.drop_target_register(tkinterdnd2.DND_FILES, tkinterdnd2.FileGroupDescriptorW,
                                        "DND_HTML", "DND_URL", tkinterdnd2.DND_TEXT)
            target.dnd_bind("<<DropEnter>>", self.on_drop_enter)
            target.dnd_bind("<<DropPosition>>", lambda e: e.action)
            target.dnd_bind("<<DropLeave>>", self.on_drop_leave)
            target.dnd_bind("<<Drop>>", self.on_drop)
            self.dnd_ok = True
        except Exception as e:
            self.dnd_ok = False
            self.set_status(tr("Drag & drop недоступен: {e}").format(e=e), error=True)
            return
        if sys.platform == "win32":
            self.build_drop_zone(tmp)
            self.start_hammer_watch()
            self.drag_watch()

    # -- перетаскивание прямо на Hammer
    # Hammer сам перетаскивание не принимает, а чужому окну цель drop не назначить (RegisterDragDrop
    # работает только внутри своего процесса). Поэтому, пока извне что-то тащат над Hammer, поверх
    # его видов показывается наше полупрозрачное окно - оно и принимает drop. Всё остальное время
    # окна нет, и клики в Hammer идут как обычно.
    def build_drop_zone(self, tmp):
        z = self.drop_zone = tk.Toplevel(self.root)
        z.withdraw()
        z.overrideredirect(True)
        z.attributes("-topmost", True)
        z.attributes("-alpha", 0.45)
        fr = self.drop_frame = tk.Frame(z, bg="#cf6a32")
        fr.pack(fill="both", expand=True)
        self.drop_label = tk.Label(fr, bg="#cf6a32", fg="white", font=(UI_FONT, 18, "bold"),
                                   justify="center")
        self.drop_label.place(relx=0.5, rely=0.5, anchor="center")
        fr.set_dropfile_tempdir(tmp)
        fr.drop_target_register(tkinterdnd2.DND_FILES, tkinterdnd2.FileGroupDescriptorW,
                                "DND_HTML", "DND_URL", tkinterdnd2.DND_TEXT)
        fr.dnd_bind("<<DropEnter>>", lambda e: e.action)
        fr.dnd_bind("<<DropPosition>>", lambda e: e.action)
        fr.dnd_bind("<<DropLeave>>", self.on_zone_leave)
        fr.dnd_bind("<<Drop>>", self.on_zone_drop)
        # Плашка с ходом дела в заголовке окна Hammer: статус-строку clip2vtf при броске не видно,
        # а без неё пауза в 2-5 с выглядела как "не сработало" и провоцировала повторный бросок.
        # В области заголовка - чтобы никогда не закрыть точку клика в 3D-виде.
        t = self.toast = tk.Toplevel(self.root)
        t.withdraw()
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        self.toast_label = tk.Label(t, font=(UI_FONT, 11, "bold"), fg="white", bg="#333333",
                                    padx=self.px(12), pady=self.px(4))
        self.toast_label.pack()
        self._toast_id = None
        self.build_size_panel()
        # поле "Декаль, ширина" поменяли в окне программы - ползунок следует за ним
        self.v["decal_units"].trace_add("write", lambda *a: self.sync_size_panel())

    # -- ползунок размера декали поверх Hammer
    # Видна, только пока в Hammer выделена infodecal, сама панель активна (её двигают/в поле
    # пишут) или идёт бросок. Размер из ползунка/поля:
    #  - всегда задаёт размер следующих брошенных картинок (это поле decal_units);
    #  - если в Hammer выделена infodecal - ей сразу ставится материал нужного размера
    #    (hammer_set_selected_decal_texture: Object Properties -> Texture -> Применить).
    SIZE_MIN, SIZE_MAX = 8, 1024  # юнитов; шкала логарифмическая - мелкие размеры не в 1 пикселе

    def _units_to_pos(self, units):
        return 100.0 * math.log(max(self.SIZE_MIN, min(self.SIZE_MAX, units)) / self.SIZE_MIN) \
            / math.log(self.SIZE_MAX / self.SIZE_MIN)

    def _pos_to_units(self, pos):
        u = self.SIZE_MIN * (self.SIZE_MAX / self.SIZE_MIN) ** (float(pos) / 100.0)
        step = 1 if u < 32 else 4 if u < 128 else 8 if u < 512 else 16
        return int(round(u / step) * step)

    def _units(self):
        try:
            return float(str(self.v["decal_units"].get()).replace(",", "."))
        except ValueError:
            return 128.0

    def build_size_panel(self):
        p = self.size_panel = tk.Toplevel(self.root)
        p.withdraw()
        p.overrideredirect(True)
        p.attributes("-topmost", True)
        bg = "#2b2b2b"
        fr = tk.Frame(p, bg=bg, padx=self.px(8), pady=self.px(3))
        fr.pack()
        tk.Label(fr, text=tr("Декаль:"), fg="white", bg=bg, font=(UI_FONT, 10, "bold")).pack(side="left")
        self.size_scale = ttk.Scale(fr, from_=0, to=100, orient="horizontal", length=self.px(200),
                                    command=self.on_size_slide)
        self.size_scale.pack(side="left", padx=self.px(6))
        self.size_entry_var = tk.StringVar()
        self.size_entry = tk.Entry(fr, textvariable=self.size_entry_var, width=6, justify="right",
                                   font=(UI_FONT, 10, "bold"), bg="#1e1e1e", fg="#ffd27f",
                                   insertbackground="white", relief="flat")
        self.size_entry.pack(side="left")
        tk.Label(fr, text=tr("юн."), fg="#ffd27f", bg=bg, font=(UI_FONT, 10, "bold")).pack(side="left",
                                                                                          padx=(self.px(3), 0))
        self.size_hint = tk.Label(fr, fg="#9a9a9a", bg=bg, font=(UI_FONT, 9))
        self.size_hint.pack(side="left", padx=(self.px(8), 0))
        # все остальные настройки текстуры/материала - выпадающей панелью (HammerOptions)
        self.opts_btn = tk.Label(fr, fg="white", bg="#3c3c3c", font=(UI_FONT, 9, "bold"),
                                 padx=self.px(8), cursor="hand2")
        self.opts_btn.pack(side="left", padx=(self.px(10), 0), fill="y")
        self.opts_btn.bind("<Button-1>", lambda e: self.toggle_hammer_opts())
        self.hopts = HammerOptions(self, self.on_hammer_option)
        self._opts_shown = False
        self._opts_after = None
        self._rebuild_pending = False
        self._refresh_after = None
        self.paint_opts_btn()
        self.size_entry.bind("<Return>", self.on_size_entry)
        self.size_entry.bind("<KP_Enter>", self.on_size_entry)
        self.size_entry.bind("<FocusOut>", self.on_size_entry)
        for w in (fr, self.size_scale):
            w.bind("<MouseWheel>", self.on_size_wheel)
        self.size_scale.bind("<ButtonRelease-1>", lambda e: self.size_committed())
        self._panel_shown = False
        self._panel_next = 0.0
        self._last_place = None     # dict(mat, base_mat, pt, tool, dirty, t) - последняя наша декаль
        self._resize_after = None   # отложенное применение (колесо мыши)
        self._resize_pending = None
        self.sync_size_panel()

    def sync_size_panel(self):
        units = self._units()
        self._size_syncing = True
        self.size_scale.set(self._units_to_pos(units))
        self._size_syncing = False
        if self.root.focus_get() is not self.size_entry:
            self.size_entry_var.set(f"{units:g}")
        self.update_size_hint()

    def update_size_hint(self):
        sel = bool(getattr(self, "hs", {}).get("sel_decal"))
        txt = tr("к выделенной декали") if sel else tr("для следующих декалей")
        if self.size_hint.cget("text") != txt:
            self.size_hint.config(text=txt)

    def on_size_slide(self, pos):
        if getattr(self, "_size_syncing", False):
            return
        units = self._pos_to_units(pos)
        self.v["decal_units"].set(str(units))
        self.size_entry_var.set(str(units))

    def on_size_entry(self, e=None):
        try:
            units = float(self.size_entry_var.get().replace(",", "."))
        except ValueError:
            self.size_entry_var.set(f"{self._units():g}")
            return "break"
        units = max(1.0, min(4096.0, units))
        if abs(units - self._units()) > 1e-6 or (e is not None and getattr(e, "keysym", "") in ("Return", "KP_Enter")):
            self.v["decal_units"].set(f"{units:g}")
            self.size_committed()
        return "break"

    def on_size_wheel(self, e):
        units = self._units()
        step = 1 if units < 32 else 4 if units < 128 else 8 if units < 512 else 16
        units = max(self.SIZE_MIN, min(self.SIZE_MAX, units + (step if e.delta > 0 else -step)))
        self.v["decal_units"].set(f"{units:g}")
        # колесо крутят много раз подряд - применить, когда перестанут
        if self._resize_after:
            self.root.after_cancel(self._resize_after)
        self._resize_after = self.root.after(400, self.size_committed)
        return "break"

    def size_committed(self):
        """Размер выбран (отпустили ползунок / Enter / перестали крутить колесо). Если в Hammer
        выделена infodecal - меняем размер ей; в любом случае это размер следующих бросков."""
        self._resize_after = None
        self.save_settings()
        if not getattr(self, "hs", {}).get("sel_decal"):
            return
        units = self._units()
        if self.hammer_busy or self.busy:
            self._resize_pending = units  # применится, когда Hammer освободится
            return
        self.resize_selected(units)

    def resize_selected(self, units):
        roots = [self.v["client_tf"].get().strip(), self.v["tf_root"].get().strip()]
        self.hammer_busy = True
        self.last_save_text = ""
        self.show_toast(tr("размер {units:g} юн.: меняю выделенную декаль...").format(units=units))
        hlog(f"--- resize selected decal -> {units:g} units")

        def work():
            try:
                ok, msg = hammer_set_selected_decal_texture(
                    lambda cur: resized_decal_material(cur, units, roots),
                    progress=lambda s: self.jobs.put(("toast", s, False)))
            except Exception as ex:
                ok, msg = False, tr("не получилось: {ex}").format(ex=ex)
            hlog(f"resize selected: ok={ok} {msg}")
            self.jobs.put(("resized", (ok, units), msg))

        threading.Thread(target=work, daemon=True).start()

    def on_resized(self, info, msg):
        ok, units = info  # units None = была пересборка с другими настройками
        self.hammer_busy = False
        self.show_toast(msg, error=not ok, hide_ms=3000 if ok else 8000)
        self.set_status(f"Hammer: {msg}", error=not ok)
        self.run_pending_hammer(units)

    def run_pending_hammer(self, last_units=None):
        """То, что поменяли, пока Hammer был занят: пересборка (она же берёт текущий размер)
        важнее отдельного размера."""
        sel = self.hs.get("sel_decal")
        if self._rebuild_pending:
            self._rebuild_pending = False
            self._resize_pending = None
            if sel:
                self.root.after(50, self.rebuild_selected)
            return
        if self._resize_pending is not None:
            pend, self._resize_pending = self._resize_pending, None
            if sel and (last_units is None or abs(pend - last_units) > 1e-6):
                self.root.after(50, lambda u=pend: self.resize_selected(u))

    # -- все настройки поверх Hammer (HammerOptions)
    def paint_opts_btn(self):
        self.opts_btn.config(text=tr("Настройки ▴") if self.v["hammer_opts_open"].get() else tr("Настройки ▾"))

    def toggle_hammer_opts(self):
        self.v["hammer_opts_open"].set(not self.v["hammer_opts_open"].get())
        self.paint_opts_btn()
        self.save_settings()
        self._panel_tick()

    def on_hammer_option(self, key):
        """Галочку/список поменяли на панели над Hammer (переменная уже записана)."""
        self.save_settings()
        # превью в окне программы - чуть позже и один раз, если щёлкают подряд
        if self._refresh_after:
            self.root.after_cancel(self._refresh_after)
        self._refresh_after = self.root.after(200, self._deferred_refresh)
        if not self.hs.get("sel_decal"):
            return
        if self._opts_after:  # щёлкают несколько галочек подряд - пересобрать один раз
            self.root.after_cancel(self._opts_after)
        self._opts_after = self.root.after(500, self.options_committed)

    def _deferred_refresh(self):
        self._refresh_after = None
        self.refresh()

    def options_committed(self):
        self._opts_after = None
        if not self.hs.get("sel_decal"):
            return
        if self.hammer_busy or self.busy:
            self._rebuild_pending = True  # применится, когда Hammer освободится
            return
        self.rebuild_selected()

    def rebuild_selected(self):
        s = {k: self.v[k].get() for k in REBUILD_KEYS + ["make_bz2", "verify"]}
        units = self._units()
        roots = [self.v["client_tf"].get().strip(), self.v["tf_root"].get().strip()]
        self.hammer_busy = True
        self.last_save_text = ""
        self.show_toast(tr("пересобираю выделенную декаль с новыми настройками..."))
        hlog(f"--- rebuild selected decal, units={units:g}, settings={s}")

        def work():
            prog = lambda t: self.jobs.put(("toast", t, False))  # noqa: E731
            try:
                ok, msg = hammer_set_selected_decal_texture(
                    lambda cur: rebuilt_decal_material(cur, s, units, roots, prog), progress=prog,
                    done=tr("декаль пересобрана"), same=tr("у декали уже такие настройки"))
            except Exception as ex:
                ok, msg = False, tr("не получилось: {ex}").format(ex=ex)
            hlog(f"rebuild selected: ok={ok} {msg}")
            self.jobs.put(("resized", (ok, None), msg))

        threading.Thread(target=work, daemon=True).start()

    def _panel_tick(self):
        """Показывать панель, пока в Hammer ВЫДЕЛЕНА infodecal (или сама панель активна / идёт
        бросок). Раньше - пока выбран инструмент декалей; пользователь: "должен появляться при
        выборе infodecal".
        Только чтение self.hs и дешёвые вызовы без сообщений в Hammer."""
        if not (self.v["size_panel"].get() and self.v["hammer_drop"].get()):
            if self._panel_shown:
                self.size_panel.withdraw()
                self._panel_shown = False
            self._show_opts(None)
            return
        ctypes, wintypes, u = _u32()
        hs = self.hs
        hpid = hs["pid"]
        fg = u.GetForegroundWindow()
        p = wintypes.DWORD()
        u.GetWindowThreadProcessId(fg, ctypes.byref(p))
        mine = False
        if p.value == os.getpid():  # наш процесс: панель/плашка/зона - да, главное окно - нет
            try:
                # только ПОКАЗАННАЯ панель: Tk при запуске отдаёт фокус последнему созданному
                # окну, даже скрытому - и панель "была активна", не будучи видна (замечено в тесте)
                mine = self._panel_shown and fg in (int(self.size_panel.wm_frame(), 16),
                                                    self.hopts.frame_id())
            except Exception:
                mine = False
            mine = mine or self._drop_shown
        want = False
        if hpid and hs["main"] and not hs["iconic"] and hs["main_rect"]:
            if mine or self._drop_shown or self.hammer_busy:
                want = True
            elif p.value == hpid:
                want = hs["sel_decal"]  # в Hammer выделена infodecal (строка статуса, фоновый поток)
        if want:
            l, t, r, _ = hs["main_rect"]
            self.size_panel.update_idletasks()
            w = self.size_panel.winfo_reqwidth()
            x = r - w - self.px(170)  # левее кнопок свернуть/развернуть/закрыть
            y = max(t, 0) + self.px(3)
            if not self._panel_shown or self.size_panel.winfo_x() != x or self.size_panel.winfo_y() != y:
                self.size_panel.geometry(f"+{x}+{y}")
            if not self._panel_shown:
                self.sync_size_panel()
                self.size_panel.deiconify()
                self._panel_shown = True
            # раскрытые настройки - под панелью, по её правому краю
            if self.v["hammer_opts_open"].get():
                ow = self.hopts.top.winfo_reqwidth()
                self._show_opts((x + w - ow, y + self.size_panel.winfo_reqheight() + self.px(2)))
            else:
                self._show_opts(None)
        else:
            if self._panel_shown:
                self.size_panel.withdraw()
                self._panel_shown = False
            self._show_opts(None)

    def _show_opts(self, pos):
        top = self.hopts.top
        if pos is None:
            if self._opts_shown:
                top.withdraw()
                self._opts_shown = False
            return
        if not self._opts_shown or (top.winfo_x(), top.winfo_y()) != pos:
            top.geometry(f"+{pos[0]}+{pos[1]}")
        if not self._opts_shown:
            top.deiconify()
            self._opts_shown = True

    def show_toast(self, text, error=False, hide_ms=None):
        if not hasattr(self, "toast"):
            return
        if self._toast_id:
            self.root.after_cancel(self._toast_id)
            self._toast_id = None
        self.toast_label.config(text=f"clip2vtf: {text}", bg="#b00020" if error else "#333333")
        self.toast.update_idletasks()
        w = self.toast.winfo_reqwidth()
        l, t, r = 0, 0, self.root.winfo_screenwidth()
        if self._hmain:
            try:
                l, t, r, _ = _hw_rect(self._hmain)
            except Exception:
                pass
        x = (l + r) // 2 - w // 2
        y = max(t, 0) + self.px(4)
        self.toast.geometry(f"+{x}+{y}")
        self.toast.deiconify()
        self.toast.lift()
        if hide_ms:
            self._toast_id = self.root.after(hide_ms, self.toast.withdraw)

    def show_drop_zone(self, rect):
        l, t, r, b = rect
        tool = HAMMER_TOOL.get(self.v["hammer_mode"].get())
        if tool == TOOL_OVERLAY:
            text = tr("Отпусти над стеной в 3D-виде -\nкартинка станет оверлеем\n(clip2vtf)")
        elif tool == TOOL_FACE:
            text = tr("Отпусти над гранью браша в 3D-виде -\nкартинка станет её текстурой\n(clip2vtf)")
        else:
            text = tr("Отпусти над стеной в 3D-виде -\nкартинка станет декалью\n(clip2vtf)")
        self.drop_label.config(text=text)
        self.drop_zone.geometry(f"{r - l}x{b - t}+{l}+{t}")
        self.drop_zone.deiconify()
        self.drop_zone.lift()
        self._drop_shown = True

    def hide_drop_zone(self):
        if self._hide_id:
            self.root.after_cancel(self._hide_id)
            self._hide_id = None
        if self._drop_shown:
            self.drop_zone.withdraw()
            self._drop_shown = False

    def on_zone_leave(self, e):
        self.hide_drop_zone()
        return e.action

    def on_zone_drop(self, e):
        pt = cursor_pos()  # процесс DPI-aware -> физические пиксели, как у Hammer
        data = e.data or ""
        hlog(f"zone drop at {pt}, type={getattr(e, 'type', '?')}, data={data[:120]!r}")
        self.hide_drop_zone()  # до клика окно должно исчезнуть, иначе клик попадёт в него
        if not (self.busy or self.hammer_busy):
            self.show_toast(tr("делаю материал..."))
        self.accept_drop(data, hammer_pt=pt)
        return e.action

    # Всё, что спрашивает Hammer (сообщения в чужой процесс), - ТОЛЬКО в этом фоновом потоке.
    # Hammer отвечает медленно (его поток занят 3D-отрисовкой): проверка инструмента - 62 мс,
    # поиск 3D-вида - 570 мс, поиск Hammer - 54 мс (замерено). Когда это шло в потоке Tk каждые
    # 30 мс, окно clip2vtf почти всё время ждало Hammer и тормозило, в т.ч. при сворачивании.
    # Поток Tk только читает готовый self.hs.
    def start_hammer_watch(self):
        global DIALOG_HIDER
        if DIALOG_HIDER is None:
            DIALOG_HIDER = _DialogHider()  # окна автоматизации Hammer - невидимыми (см. класс)
        self.hs = dict(pid=None, main=None, fg=0, fg_pid=0, tool=False, views=None,
                       main_rect=None, iconic=False, sel="", sel_decal=False)
        self._closing = False
        self._tool_id = 33008

        def loop():
            ctypes, wintypes, u = _u32()
            next_find = next_mdi = 0.0
            mdi = view = active = None
            rm = None  # буфер в памяти Hammer для чтения строки статуса
            while not self._closing:
                try:
                    now = time.time()
                    main = self.hs["main"]
                    if not main or not u.IsWindow(main):
                        main = None
                        if now >= next_find:
                            next_find = now + 0.25
                            pid, main = hammer_find()
                            self.hs.update(pid=pid, main=main)
                            mdi = view = active = None
                            if rm:
                                rm.close()
                            rm = None
                    fg = u.GetForegroundWindow()
                    p = wintypes.DWORD()
                    u.GetWindowThreadProcessId(fg, ctypes.byref(p))
                    upd = dict(fg=fg, fg_pid=p.value)
                    if main:
                        upd.update(iconic=bool(u.IsIconic(main)), main_rect=_hw_rect(main))
                        if not mdi or not u.IsWindow(mdi):
                            mdi = _hw_find_desc(main, lambda h: _hw_class(h) == "MDIClient")
                        if mdi and now >= next_mdi:  # сменилась активная карта -> заново найти 3D-вид
                            next_mdi = now + 1.0
                            cur = _hw_send(mdi, 0x0229, 0, 0, timeout=500)  # WM_MDIGETACTIVE
                            if cur != active or not view or not u.IsWindow(view):
                                active = cur
                                view, _ = hammer_3d_view(main)
                        upd["views"] = _hw_rect(view) if view and u.IsWindow(view) else None
                        if p.value == self.hs["pid"] or self._panel_shown:
                            upd["tool"] = hammer_tool_checked(main, self._tool_id)
                            # что выделено: вторая ячейка строки статуса ("infodecal  [ID: ...]")
                            if rm is None or rm.pid != self.hs["pid"]:
                                rm = RemoteMem(self.hs["pid"], 4096)
                            sel = hammer_selection_text(main, rm)
                            upd.update(sel=sel, sel_decal=sel.lower().startswith("infodecal"))
                    else:
                        upd.update(tool=False, views=None, main_rect=None, iconic=False, sel="", sel_decal=False)
                    self.hs.update(upd)
                except Exception:
                    pass
                time.sleep(0.02)

        threading.Thread(target=loop, daemon=True, name="hammer-watch").start()

    def drag_watch(self):
        try:
            self._drag_tick()
        except Exception:
            pass
        self._watch_id = self.root.after(15, self.drag_watch)

    def _drag_tick(self):
        hs = self.hs
        self._hpid, self._hmain = hs["pid"], hs["main"]
        self._tool_id = HAMMER_TOOL.get(self.v["hammer_mode"].get(), 33008)
        if hasattr(self, "size_panel"):
            self._panel_tick()
        if not self.v["hammer_drop"].get():
            self.hide_drop_zone()
            return
        down = lbutton_down()
        pt = cursor_pos()
        if down and not self._was_down:
            # Кнопку только что нажали. Перетаскивание извне = нажали НЕ над Hammer и не над нами.
            pid = root_pid_at(pt)  # без сообщений в Hammer - мгновенно
            self._press_outside = bool(self._hmain) and pid not in (self._hpid, os.getpid())
        if down and self._press_outside and not self._drop_shown and self._hmain:
            # Под курсором сам Hammer (а не, например, перетаскиваемое окно браузера) -
            # значит, это OLE-перетаскивание картинки/файла над ним.
            rect = hs["views"]  # 3D-вид из фонового потока
            if rect and rect[0] <= pt[0] < rect[2] and rect[1] <= pt[1] < rect[3] \
                    and root_pid_at(pt) == self._hpid:
                # Только настоящее перетаскивание (курсор OLE drag, см. ole_drag_cursor): раньше
                # зона выскакивала, когда над Hammer тащили окно Discord/браузера или выделяли текст.
                if ole_drag_cursor():
                    hlog(f"drop zone shown, cursor {cursor_handle():#x}")
                    self.show_drop_zone(rect)
        if not down and self._drop_shown and not self._hide_id:
            # Отпустили не над зоной (или отменили Esc): убрать чуть позже, чтобы успел прийти Drop.
            self._hide_id = self.root.after(400, self.hide_drop_zone)
        self._was_down = down

    def on_drop_enter(self, e):
        self.canvas.config(highlightthickness=3, highlightbackground="#cf6a32")
        self.set_status(tr("Отпусти, чтобы загрузить картинку"))
        return e.action

    def on_drop_leave(self, e):
        self.canvas.config(highlightthickness=0)
        return e.action

    def on_drop(self, e):
        self.canvas.config(highlightthickness=0)
        self.accept_drop(e.data or "")
        return e.action

    def accept_drop(self, data, hammer_pt=None):
        """Разобрать перетащенное (файлы / HTML / ссылка). hammer_pt: отпустили над Hammer -
        после загрузки картинка сразу ставится туда (см. set_source -> place_in_hammer)."""
        if self.busy or self.hammer_busy:
            self.set_status(tr("Подожди, предыдущая картинка ещё обрабатывается"), error=True)
            if hammer_pt:
                hlog("drop refused: previous one still in progress")
                self.show_toast(tr("подожди - ещё ставлю предыдущую картинку"), error=True, hide_ms=4000)
            return
        self.pending_drop = hammer_pt
        # список файлов: {C:/a b.png} C:/c.png
        try:
            items = self.root.tk.splitlist(data)
        except Exception:
            items = [data]
        for it in items:
            if os.path.isfile(it):
                try:
                    self.set_source(open_path(it), [file_stem(it)])
                    return
                except Exception:
                    continue
        src = find_image_source(data)
        if src is None:
            self.pending_drop = None
            self.set_status(tr("Не нашёл картинку в том, что перетащили"), error=True)
        elif src[0] == "path":
            self.load_file(src[1])
        else:
            self.load_url(src[1], names_from_html(data))
        # load_file/load_url при ошибке pending_drop не трогают - сбрасываем в poll_jobs/load_file

    # -- загрузка по ссылке в фоне, чтобы окно не зависало
    def load_url(self, url, names=()):
        if self.busy:
            self.set_status(tr("Уже скачиваю, подожди"), error=True)
            return
        self.busy = True
        short = url if len(url) < 80 else url[:77] + "..."
        self.set_status(tr("Скачиваю: {short}").format(short=short))
        self.root.config(cursor="watch")
        names = list(names) + names_from_url(url)

        def work():
            try:
                self.jobs.put(("ok", fetch_image(url), names))
            except Exception as ex:
                self.jobs.put(("err", tr("Не скачалось: {ex}").format(ex=ex), None))

        threading.Thread(target=work, daemon=True).start()

    def poll_jobs(self):
        try:
            while True:
                kind, a, b = self.jobs.get_nowait()
                if kind == "toast":  # ход постановки в Hammer из рабочего потока
                    self.show_toast(a, error=b)
                    continue
                if kind == "resized":  # ((ok, пересоздана, материал, юниты), сообщение)
                    self.on_resized(a, b)
                    continue
                if kind == "hammer":  # (ok, сообщение) из send_to_hammer / place_in_hammer
                    self.hammer_busy = False
                    placing = getattr(self, "_placing", None)
                    self._placing = None
                    if placing and hasattr(self, "size_panel"):
                        mat, pt, tool, units = placing
                        if a:  # эту декаль можно будет сразу пересоздать в другом размере
                            self._last_place = dict(mat=mat, base_mat=mat, pt=pt, tool=tool,
                                                    units=units, dirty=False, t=time.time())
                        self.update_size_hint()
                        self.run_pending_hammer()  # крутили размер / настройки во время броска
                    if hasattr(self, "toast") and self.toast.winfo_viewable():
                        self.show_toast(b if not a else b.split(". ")[-1], error=not a,
                                        hide_ms=3000 if a else 8000)
                    self.set_status(f"{self.last_save_text + '. ' if self.last_save_text else ''}"
                                    f"Hammer: {b}", error=not a)
                    self.last_save_text = ""
                    continue
                self.busy = False
                self.root.config(cursor="")
                if kind == "ok":
                    self.set_source(a, b)
                else:
                    self.pending_drop = None
                    self.set_status(a, error=True)
        except queue.Empty:
            pass
        self._poll_id = self.root.after(20, self.poll_jobs)

    def on_option(self):
        self.apply_topmost()
        self.refresh()
        self.save_settings()

    def apply_topmost(self):
        self.root.attributes("-topmost", bool(self.v["topmost"].get()))

    def show_help(self):
        messagebox.showinfo("clip2vtf", HELP, parent=self.root)
        return "break"

    def text_field(self):
        w = self.root.focus_get()
        return w if isinstance(w, (tk.Entry, ttk.Entry)) and not isinstance(w, ttk.Combobox) else None

    def on_ctrl_key(self, e):
        shift = bool(e.state & 0x1)
        k = e.keycode
        latin = e.keysym.lower() in ("v", "c", "x", "a")  # Tk уже обработал сам
        field = self.text_field()
        if k == VK["V"] and not shift:
            if field is not None and not clipboard_has_image():
                if not latin:
                    field.event_generate("<<Paste>>")
                return "break"
            return self.paste()
        if field is not None and k in (VK["C"], VK["X"]) and not shift:
            if not latin:
                field.event_generate("<<Copy>>" if k == VK["C"] else "<<Cut>>")
            return "break"
        if field is not None and k == VK["A"] and not shift:
            field.select_range(0, "end")
            field.icursor("end")
            return "break"
        if k == VK["S"]:
            return self.save_as() if shift else self.save()
        if k == VK["C"] and shift:
            self.copy_path()
        elif k == VK["O"] and not shift:
            self.open_file()
        elif k == VK["E"] and not shift:
            self.open_folder()
        elif k == VK["H"] and not shift:
            self.send_to_hammer()
        elif k == VK["Q"] and not shift:
            self.close()
        else:
            return None
        return "break"

    # -- ввод
    def set_source(self, img, names=None):
        if isinstance(names, str):
            names = [names]
        self.src = img
        self.names = list(names or [])
        self._prep_key = None
        if self.v["auto_name"].get() or not clean_name(self.v["name"].get()):
            self.v["name"].set(pick_name(self.names) or time.strftime("clip_%Y%m%d_%H%M%S"))
        self.refresh()
        self.set_status(tr("Загружено: {width}x{height}, имя: {name}").format(width=img.width, height=img.height, name=self.v['name'].get()))
        if self.pending_drop:  # отпустили над Hammer - сразу на стену (autosave не нужен)
            pt, self.pending_drop = self.pending_drop, None
            self.root.after(10, lambda: self.place_in_hammer(pt))
        elif self.v["autosave"].get():
            self.root.after(10, self.save)

    def paste(self):
        self.pending_drop = None
        kind, a, names = grab_clipboard(self.root)
        if kind == "error":
            self.set_status(a, error=True)
        elif kind == "url":
            self.load_url(a, names)
        else:
            self.set_source(a, names)
        return "break"

    def open_file(self):
        p = filedialog.askopenfilename(filetypes=[(tr("Картинки"), "*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tga *.tif *.tiff"),
                                                  (tr("Все файлы"), "*.*")])
        if p:
            self.pending_drop = None
            self.load_file(p)

    def load_file(self, p):
        try:
            self.set_source(open_path(p), [file_stem(p)])
        except Exception as e:
            self.pending_drop = None
            self.set_status(tr("Не открылось: {e}").format(e=e), error=True)

    def pick_tf(self):
        p = filedialog.askdirectory(initialdir=self.v["tf_root"].get())
        if p:
            self.v["tf_root"].set(os.path.normpath(p))

    def pick_client(self):
        p = filedialog.askdirectory(initialdir=self.v["client_tf"].get())
        if p:
            self.v["client_tf"].set(os.path.normpath(p))

    # -- расчёт
    def settings(self):
        out = {k: v.get() for k, v in self.v.items() if k != "name"}
        out["favorites"] = self.favorites
        try:
            out["tab"] = self.nb.select()
        except Exception:
            out["tab"] = self.saved_tab
        return out

    def refresh_controls(self):
        shader = self.v["shader"].get()
        for w in self.decal_entries:
            w.config(state="normal" if shader == DECAL else "disabled")
        for h in self.shader_hints:
            h.config(text=SHADER_HINTS.get(shader, ""))

    def chosen_fmt(self, akind):
        return pick_fmt(self.v["fmt"].get(), akind)

    def prepared(self):
        key = (id(self.src), self.v["flip_h"].get(), self.v["bg_remove"].get(), self.v["trim"].get())
        if key != self._prep_key:
            self._prep = prepare(self.src, *key[1:])
            self._prep_key = key
        return self._prep

    def refresh(self):
        self.refresh_controls()
        if self.src is None:
            return
        img = self.prepared()
        W, H = target_size(img.width, img.height, self.v["width"].get(), self.v["height"].get())
        res = fit_resize(img, W, H, self.v["fit"].get(), self.v["pixel_art"].get())
        if self.v["bleed"].get():
            res = bleed_colors(res)
        self.result = res
        akind = alpha_kind(res)
        fmt = self.chosen_fmt(akind)
        warn = ""
        if akind != "none" and fmt in NO_ALPHA_FORMATS:
            warn = tr("\n! В картинке есть прозрачность, а формат без альфы - она пропадёт.")
        if self.v["pixel_art"].get() and fmt in (ImageFormats.DXT1, ImageFormats.DXT5):
            warn += tr("\n! Для пиксель-арта лучше BGRA8888: DXT размывает края блоков 4x4.")
        akname = {"none": tr("нет"), "binary": tr("есть (только 0/255)"), "soft": tr("есть (плавная)")}[akind]
        bpp = {ImageFormats.DXT1: 0.5, ImageFormats.DXT5: 1, ImageFormats.BGRA8888: 4,
               ImageFormats.BGR888: 3}[fmt]
        size = W * H * bpp * (4 / 3 if self.v["mips"].get() else 1)
        self.info.config(text=tr("Исходник: {width}x{height}").format(width=self.src.width, height=self.src.height)
                              + (tr(" -> {width}x{height} после обрезки").format(width=img.width, height=img.height) if img.size != self.src.size else "")
                              + tr("\nРезультат: {W}x{H}, {format}, ~{kb:.0f} КБ\nПрозрачность: {akname}{warn}").format(W=W, H=H, format=fmt.name, kb=size / 1024, akname=akname, warn=warn) + self.decal_info(W, H))
        self.show_preview()

    def decal_info(self, W, H):
        if self.v["shader"].get() != DECAL:
            return ""
        sc = decal_scale(self.v["decal_units"].get(), W)
        if sc is None:
            return tr("\n! Ширина декали должна быть числом > 0")
        return tr("\nДекаль в игре: {w:.0f} x {h:.0f} юнитов ($decalscale {sc:g})").format(w=W * sc, h=H * sc, sc=sc)

    def on_canvas_configure(self, e=None):
        if self._prev_after:
            self.root.after_cancel(self._prev_after)
        self._prev_after = self.root.after(30, self.show_preview)

    def show_preview(self):
        self._prev_after = None
        if self.result is None or self.root.state() == "iconic":
            return  # свёрнутое окно не перерисовываем
        cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        img = self.result
        k = min(cw / img.width, ch / img.height)
        size = (max(1, int(img.width * k)), max(1, int(img.height * k)))
        key = (id(img), size, self.v["pixel_art"].get())
        if self._prev_cache and self._prev_cache[0] == key and getattr(self, "_prev_shown", None) == (key, cw, ch):
            return  # уже нарисовано ровно это - холст сам хранит картинку, пересоздавать нечего
        if not self._prev_cache or self._prev_cache[0] != key:
            # reducing_gap: сначала быстрое целочисленное уменьшение, потом точный фильтр -
            # то же качество превью, в разы быстрее полного LANCZOS по 2048x1024
            prev = img.resize(size, Image.NEAREST if k >= 1 or self.v["pixel_art"].get() else Image.LANCZOS,
                              reducing_gap=2.0)
            bg = checker(size) if alpha_kind(self.result) != "none" else Image.new("RGB", size)
            bg.paste(prev, (0, 0), prev)
            self._prev_cache = (key, ImageTk.PhotoImage(bg))
        self.photo = self._prev_cache[1]
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2, image=self.photo)
        self._prev_shown = (key, cw, ch)

    # -- вывод
    def material_path(self):
        sub, name = clean_subdir(self.v["subdir"].get()), clean_name(self.v["name"].get())
        return (sub + "/" + name) if sub else name

    def mat_file(self, root, mat, ext):
        return os.path.join(root, "materials", *mat.split("/")) + ext

    def save(self, shader_override=None, force_unique=False, to_client=False, send=True):
        """shader_override/force_unique/to_client - для картинки, брошенной в Hammer: материал
        декали, не затирать существующие, обязательно в клиентский tf. send=False: в Hammer не
        отправлять (этим займётся place_in_hammer). Итог удачного сохранения - self.last_saved."""
        self.last_saved = None
        if isinstance(shader_override, tk.Event):  # вызов из биндинга клавиши
            shader_override = None
        shader = shader_override or self.v["shader"].get()
        if self.result is None:
            self.set_status(tr("Сначала вставь картинку (Ctrl+V)"), error=True)
            return "break"
        name = clean_name(self.v["name"].get())
        if not name:
            self.set_status(tr("Пустое имя"), error=True)
            return "break"
        self.v["name"].set(name)
        tf = self.v["tf_root"].get().strip()
        if not os.path.isdir(tf):
            self.set_status(tr("Нет папки tf: {tf}").format(tf=tf), error=True)
            return "break"
        mat = self.material_path()

        def taken(m):
            return os.path.exists(self.mat_file(tf, m, ".vtf")) or os.path.exists(self.mat_file(tf, m, ".vmt"))

        if taken(mat) and (self.v["unique_name"].get() or force_unique):
            # имя уже с номером (soldier_3) -> следующий номер, а не soldier_3_2
            m = re.fullmatch(r"(.+?)_(\d+)", name)
            base, n = (m.group(1), int(m.group(2)) + 1) if m else (name, 2)
            sub = clean_subdir(self.v["subdir"].get())
            while taken(f"{sub}/{base}_{n}" if sub else f"{base}_{n}"):
                n += 1
            name = f"{base}_{n}"
            self.v["name"].set(name)
            mat = self.material_path()
        elif taken(mat) and self.v["confirm_overwrite"].get():
            if not messagebox.askyesno(tr("Перезаписать?"), tr("materials/{mat} уже есть. Перезаписать?").format(mat=mat)):
                return "break"
        vtf_path = self.mat_file(tf, mat, ".vtf")

        self.root.config(cursor="watch")
        self.root.update()
        try:
            s = {k: self.v[k].get() for k in REBUILD_KEYS}
            data, vmt, fmt, akind = encode_material(self.result, s, mat, shader,
                                                    self.v["decal_units"].get())
            targets = [tf]
            if self.v["mirror_client"].get() or self.v["send_hammer"].get() or to_client:  # Hammer читает клиентский tf
                cl = self.v["client_tf"].get().strip()
                if os.path.isdir(cl) and os.path.normcase(os.path.abspath(cl)) != os.path.normcase(os.path.abspath(tf)):
                    targets.append(cl)
            # для Hammer: новый ли это для клиентского tf материал (тогда браузер надо "прогреть")
            hammer_new = not os.path.exists(self.mat_file(targets[-1], mat, ".vmt"))
            for root in targets:
                vp = self.mat_file(root, mat, ".vtf")
                os.makedirs(os.path.dirname(vp), exist_ok=True)
                with open(vp, "wb") as f:
                    f.write(data)
                with open(vp[:-4] + ".vmt", "w", encoding="utf-8", newline="") as f:
                    f.write(vmt)
                if root != tf:
                    continue  # клиенту .bz2 не нужны, FastDL раздаёт только сервер
                for p, raw in ((vp + ".bz2", data), (vp[:-4] + ".vmt.bz2", vmt.encode("utf-8"))):
                    if self.v["make_bz2"].get():
                        with open(p, "wb") as f:
                            f.write(bz2.compress(raw, 9))
                    elif self.v["delete_stale_bz2"].get() and os.path.exists(p):
                        os.remove(p)
            if self.v["save_source"].get():
                sp = os.path.join(tf, "materialsrc", *mat.split("/")) + ".png"
                os.makedirs(os.path.dirname(sp), exist_ok=True)
                self.src.save(sp)
            has_alpha = fmt not in NO_ALPHA_FORMATS and akind != "none"
            if self.v["verify"].get():
                db, msg = valve_check(vtf_path, self.result, has_alpha)
            else:
                db, msg = None, tr("не проверялось")
        except Exception as e:
            self.root.config(cursor="")
            self.set_status(tr("Ошибка: {e}").format(e=e), error=True)
            return "break"
        self.root.config(cursor="")
        where = (" + .bz2" if self.v["make_bz2"].get() else "") + (tr(" (+ клиент)") if len(targets) > 1 else "")
        text = tr("Сохранено{where}: materials/{mat}.vtf/.vmt, {kb} КБ. vtf2tga: {msg}").format(where=where, mat=mat, kb=len(data) // 1024, msg=msg)
        if self.v["copy_path_after_save"].get():
            try:
                set_clipboard_text(self.root, mat)
                text += tr(". Путь скопирован")
            except OSError:
                text += tr(". Путь НЕ скопирован: буфер занят")
        bad = db is not None and db < MIN_PSNR
        if bad:
            text += tr(" - ПЛОХО, попробуй BGRA8888")
        self.set_status(text, error=bad)
        if self.v["open_folder_after_save"].get():
            os.startfile(os.path.dirname(vtf_path))
        self.save_settings()
        # исходник - для пересборки этой декали с другими настройками прямо из Hammer
        threading.Thread(target=save_source_copy, args=(self.src, mat), daemon=True).start()
        self.last_saved = (mat, hammer_new, text)
        if send and self.v["send_hammer"].get():
            self.last_save_text = text
            self.send_to_hammer(quiet_if_absent=True, new_file=hammer_new)
        return "break"

    def place_in_hammer(self, pt):
        """Картинку отпустили над Hammer: сохранить материал и поставить на стену - декалью,
        текстурой грани или оверлеем (настройка "В Hammer как")."""
        if self.hammer_busy:
            self.set_status(tr("Hammer ещё занят предыдущей картинкой"), error=True)
            return
        tool = HAMMER_TOOL.get(self.v["hammer_mode"].get(), HAMMER_TOOL[HAMMER_MODES[0]])
        # текстуре браша нужен обычный материал стены, без $decal (с ним Hammer рисует грань
        # как декаль, а игра - вообще не рисует)
        shader = "LightmappedGeneric" if tool == TOOL_FACE else DECAL
        self.save(shader_override=shader, force_unique=True, to_client=True, send=False)
        if not self.last_saved:
            hlog(f"save failed: {self.status.cget('text')}")
            self.show_toast(self.status.cget("text")[:120], error=True, hide_ms=7000)
            return  # save() уже написал, что не так
        mat, new_file, text = self.last_saved
        # станет _last_place, если постановка удастся (poll_jobs) - для пересоздания в другом размере
        self._placing = (mat, pt, tool, self._units() if hasattr(self, "size_panel") else 0)
        self.hammer_busy = True
        self.last_save_text = text
        self.set_status(tr("{text}. Ставлю в Hammer...").format(text=text))
        face = {k: self.v["brush_" + k].get() for k in ("mode", "scale", "align", "rotate", "luxel")}

        def work():
            try:
                ok, msg = hammer_place(mat, new_file, tool, pt, face=face,
                                       progress=lambda s: self.jobs.put(("toast", s, False)))
            except Exception as ex:
                ok, msg = False, tr("ошибка: {ex}").format(ex=ex)
            self.jobs.put(("hammer", ok, msg))

        threading.Thread(target=work, daemon=True).start()

    def send_to_hammer(self, quiet_if_absent=False, new_file=False):
        """Сделать текущий материал текущей текстурой Hammer++ (в фоне, окно не зависает)."""
        if getattr(self, "hammer_busy", False):
            return "break"
        mat = self.material_path()
        roots = [self.v["client_tf"].get().strip(), self.v["tf_root"].get().strip()]
        if not any(os.path.isfile(self.mat_file(r, mat, ".vmt")) for r in roots if r):
            self.set_status(tr("materials/{mat}.vmt ещё не сохранён - сначала Ctrl+S").format(mat=mat), error=True)
            return "break"
        if sys.platform != "win32":
            return "break"
        hs = getattr(self, "hs", None)
        pid = hs["pid"] if hs and hs["main"] else hammer_find()[0]
        if not pid:
            if not quiet_if_absent:
                self.set_status(tr("Hammer++ не запущен"), error=True)
            self.last_save_text = ""
            return "break"
        self.hammer_busy = True
        self.set_status(tr("Отправляю {mat} в Hammer...").format(mat=mat))

        def work():
            try:
                ok, msg = hammer_apply(mat, new_file=new_file)
            except Exception as ex:
                ok, msg = False, tr("ошибка: {ex}").format(ex=ex)
            self.jobs.put(("hammer", ok, msg))

        threading.Thread(target=work, daemon=True).start()
        return "break"

    def save_as(self):
        if self.result is None:
            self.set_status(tr("Сначала вставь картинку (Ctrl+V)"), error=True)
            return "break"
        start = os.path.join(self.v["tf_root"].get(), "materials", *clean_subdir(self.v["subdir"].get()).split("/"))
        while start and not os.path.isdir(start) and os.path.dirname(start) != start:
            start = os.path.dirname(start)
        p = filedialog.asksaveasfilename(parent=self.root, initialdir=start or None,
                                         initialfile=clean_name(self.v["name"].get()) + ".vtf",
                                         defaultextension=".vtf", filetypes=[("VTF", "*.vtf")],
                                         confirmoverwrite=False)
        if not p:
            return "break"
        tf, sub, name = split_material_path(p)
        if tf is None:
            self.set_status(tr("Файл должен лежать внутри папки ...\\materials\\"), error=True)
            return "break"
        self.v["tf_root"].set(tf)
        self.v["subdir"].set(sub)
        self.v["name"].set(name)
        return self.save()

    def copy_path(self):
        try:
            set_clipboard_text(self.root, self.material_path())
        except OSError as e:
            self.set_status(tr("Не скопировалось: {e}").format(e=e), error=True)
            return
        self.set_status(tr("Скопировано: {path}").format(path=self.material_path()))

    def open_folder(self):
        d = os.path.join(self.v["tf_root"].get(), "materials", *clean_subdir(self.v["subdir"].get()).split("/"))
        if os.path.isdir(d):
            os.startfile(d)
        else:
            self.set_status(tr("Папки ещё нет: {d}").format(d=d), error=True)

    def set_status(self, text, error=False):
        self.status.config(text=text, foreground="#b00020" if error else "")

    def save_settings(self):
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(self.settings(), f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def close(self):
        self._closing = True  # останавливает фоновый опрос Hammer
        self.save_settings()
        for aid in ("_poll_id", "_watch_id", "_opts_after", "_refresh_after"):
            try:
                self.root.after_cancel(getattr(self, aid))
            except Exception:
                pass
        self.root.destroy()


def selftest(out_path):
    """clip2vtf.exe --selftest report.txt: проверка сборки без окна - есть ли быстрый DXT-энкодер
    (Cython libsquish из srctools), грузится ли tkdnd, находится ли TF2 / vtf2tga и читает ли
    Valve то, что мы пишем."""
    lines = []
    try:
        lines.append(f"frozen={FROZEN} data={DATA}")
        lines.append(f"tf2={TF2_DIR or '-'} vtf2tga={find_vtf2tga() or '-'}")
        fast = _srctools_vtf._cy_format_funcs is not _srctools_vtf._py_format_funcs
        lines.append(f"cython_libsquish={fast}")
        langs = sorted(f[:-5] for f in os.listdir(os.path.join(RES, "lang")) if f.endswith(".json"))
        lines.append(f"languages={','.join(langs)} ui={LANG} sample={tr('Сохранить')!r}")
        img = Image.new("RGBA", (256, 128), (30, 120, 220, 255))
        ImageDraw.Draw(img).ellipse((40, 20, 200, 110), fill=(250, 200, 40, 255))
        s = {k: DEFAULTS[k] for k in REBUILD_KEYS}
        t0 = time.time()
        data, vmt, fmt, akind = encode_material(render_image(img, s), s, "selftest/x", DECAL, 64)
        lines.append(f"encode {fmt.name} {len(data)} bytes {time.time() - t0:.2f}s")
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x.vtf")
            with open(p, "wb") as f:
                f.write(data)
            db, msg = valve_check(p, render_image(img, s), False)
        lines.append(f"vtf2tga: {msg}")
        root = tk.Tk()
        root.withdraw()
        try:
            TkinterDnD._require(root)
            lines.append("tkdnd=ok")
        except Exception as e:
            lines.append(f"tkdnd=FAIL {e}")
        root.destroy()
        lines.append("OK")
    except Exception as e:
        import traceback
        lines.append("FAIL " + traceback.format_exc())
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2])
        sys.exit(0)
    enable_dpi_awareness()
    root = tk.Tk()
    app = App(root)
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        app.load_file(sys.argv[1])
        # картинка, переданная при смене языка (set_language), - во временной папке: убрать
        d = os.path.dirname(os.path.abspath(sys.argv[1]))
        if os.path.basename(d).startswith("clip2vtf_lang_") and \
                os.path.normcase(os.path.dirname(d)) == os.path.normcase(os.path.abspath(tempfile.gettempdir())):
            shutil.rmtree(d, ignore_errors=True)
    root.mainloop()
