"""Главное окно PC MCP Connect: статус, ссылка для Notion, кнопки + значок в трее.

Тёмная графитовая тема. Кнопки, поля и карточки рисуются через PIL со сглаженными
углами, поэтому выглядят одинаково на Windows 10 и 11.
Окно открывается сразу после запуска (кроме автозапуска с --minimized).
Крестик прячет окно к часам, программа продолжает работать.
Повторный запуск exe просто показывает уже открытую программу.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

# ---------------------------------------------------------------- дизайн-токены
# Графит (без «почти чёрного»), один синий акцент для главного действия.
# Зелёный, янтарный и красный используются только для состояния связи.
BG = "#1E1F22"         # фон окна
SURFACE = "#26282C"    # карточки, выпадающий список
RAISED = "#303237"     # поля и второстепенные кнопки
HOVER = "#3A3D43"
PRESSED = "#2A2C30"
BORDER = "#3A3C42"
TEXT = "#ECEDEF"
TEXT2 = "#A6A9B0"      # пояснения, контраст 7:1
TEXT3 = "#868A92"      # самое тихое, 4.7:1
ACCENT = "#2F6FCB"     # белый текст на нём — 5:1
ACCENT_H = "#3A7BD8"
ACCENT_P = "#285FAF"
ACCENT_TXT = "#7DB0F0"  # акцент как цвет текста и рамки фокуса
DANGER = "#C4453A"
DANGER_H = "#D05246"
DANGER_P = "#A83A30"
GREEN = "#4CC38A"
AMBER = "#E5A445"
RED = "#EF6B5E"
WHITE = "#FFFFFF"

PHASES = {
    "online": ("Работает", GREEN),
    "connecting": ("Подключаюсь…", AMBER),
    "error": ("Есть проблема", RED),
}
CONTENT_W = 480   # ширина колонки в логических пикселях
GUTTER = 24
SS = 4            # сглаживание: рисуем в 4 раза крупнее и уменьшаем
LANCZOS = Image.Resampling.LANCZOS


def _rgb(c: str) -> tuple:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def mix(a: str, b: str, t: float) -> str:
    return "#%02X%02X%02X" % tuple(round(x + (y - x) * t) for x, y in zip(_rgb(a), _rgb(b)))


def _colorref(c: str) -> int:
    r, g, b = _rgb(c)
    return r | (g << 8) | (b << 16)


def rounded(w: int, h: int, r: int, fill: str, bg: str, border: str | None = None, bw: int = 1):
    """Скруглённый прямоугольник со сглаженными краями на непрозрачном фоне bg."""
    w, h = max(1, int(w)), max(1, int(h))
    W, H = w * SS, h * SS
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    R = max(0, min(int(r) * SS, H // 2, W // 2))
    if border:
        d.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=border)
        o = max(1, int(bw)) * SS
        d.rounded_rectangle((o, o, W - 1 - o, H - 1 - o), radius=max(0, R - o), fill=fill)
    else:
        d.rounded_rectangle((0, 0, W - 1, H - 1), radius=R, fill=fill)
    return im.resize((w, h), LANCZOS)


# ---------------------------------------------------------------- Windows: «покажи окно», DPI, тёмный заголовок

def _event_name(m) -> str:
    h = hashlib.sha1(str(m.LOCAL).lower().encode()).hexdigest()[:12]
    return "Local" + chr(92) + "PcMcpConnect-show-" + h


def _k32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateEventW.restype = ctypes.c_void_p
    k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
    k32.OpenEventW.restype = ctypes.c_void_p
    k32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    k32.SetEvent.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k32.WaitForSingleObject.restype = ctypes.c_uint32
    return k32


def signal_existing(m) -> bool:
    """Попросить уже запущенную копию показать окно. True, если получилось."""
    if os.name != "nt":
        return False
    try:
        k32 = _k32()
        h = k32.OpenEventW(0x0002, 0, _event_name(m))  # EVENT_MODIFY_STATE
        if not h:
            return False
        ok = k32.SetEvent(h)
        k32.CloseHandle(h)
        return bool(ok)
    except Exception:  # noqa: BLE001
        return False


def _dpi_aware() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def _animations_enabled() -> bool:
    """Уважаем выключенные анимации Windows (Спец. возможности → Эффекты анимации)."""
    if os.name != "nt":
        return True
    try:
        v = ctypes.c_int(1)
        ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(v), 0)  # SPI_GETCLIENTAREAANIMATION
        return bool(v.value)
    except Exception:  # noqa: BLE001
        return True


def dark_titlebar(win) -> None:
    """Тёмная системная рамка окна; на Windows 11 заголовок сливается с фоном."""
    if os.name != "nt":
        return
    try:
        win.update_idletasks()
        u = ctypes.windll.user32
        u.GetParent.restype = ctypes.c_void_p
        u.GetParent.argtypes = [ctypes.c_void_p]
        hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_int),
                                              ctypes.c_uint]
        on = ctypes.c_int(1)
        if dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4) != 0:   # DWMWA_USE_IMMERSIVE_DARK_MODE
            dwm.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(on), 4)        # старые сборки Windows 10
        for attr, col in ((35, BG), (36, TEXT), (34, BORDER)):              # caption / text / border (Win 11)
            v = ctypes.c_int(_colorref(col))
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- элементы

class Btn(tk.Label):
    """Кнопка со скруглёнными углами: primary / secondary / ghost / danger.

    Наведение, нажатие, выключенное состояние и рамка фокуса для клавиатуры (Tab, Enter, Пробел).
    """

    def __init__(self, master, ui, text, command, kind="secondary", width=0, height=36, bg=BG, font=None,
                 hover_fg=None, radius=8):
        self.ui, self.command, self.kind, self.bgc = ui, command, kind, bg
        if kind == "primary":
            st = dict(fill=ACCENT, hover=ACCENT_H, press=ACCENT_P, fg=WHITE, border=None)
        elif kind == "danger":
            st = dict(fill=DANGER, hover=DANGER_H, press=DANGER_P, fg=WHITE, border=None)
        elif kind == "ghost":
            st = dict(fill=bg, hover=mix(bg, WHITE, 0.07), press=mix(bg, WHITE, 0.035), fg=TEXT2, border=None)
        else:
            st = dict(fill=RAISED, hover=HOVER, press=PRESSED, fg=TEXT, border=BORDER)
        self.st = st
        self.hover_fg = hover_fg or (TEXT if kind == "ghost" else None)
        self.fnt = font or ui.F_BTN
        self.hpx = ui.px(height)
        self.rpx = ui.px(radius)
        self.minw = ui.px(width) if width else 0
        self.wpx = self.minw
        self.enabled = True
        self._hover = self._down = self._focus = False
        self._fg = None
        super().__init__(master, bd=0, padx=0, pady=0, highlightthickness=0, bg=bg, compound="center",
                         font=self.fnt, cursor="hand2", takefocus=1)
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, down=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(down=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", lambda e: self._set(focus=True))
        self.bind("<FocusOut>", lambda e: self._set(focus=False))
        self.bind("<Return>", lambda e: self.invoke())
        self.bind("<space>", lambda e: self.invoke())
        self.set_text(text)

    def set_text(self, text: str, fg: str | None = None) -> None:
        self._fg = fg
        self.wpx = max(self.minw, self.ui.measure(self.fnt, text) + self.ui.px(32))
        self.config(text=text)
        self._render()

    def set_enabled(self, on: bool) -> None:
        on = bool(on)
        if on != self.enabled:
            self.enabled = on
            self.config(takefocus=1 if on else 0)
            self._render()

    def invoke(self) -> None:
        if self.enabled and self.command:
            self.command()

    def _release(self, _e=None) -> None:
        fire = self._down and self._hover
        self._set(down=False)
        if fire:
            self.invoke()

    def _set(self, **kw) -> None:
        changed = False
        for k, v in kw.items():
            if getattr(self, "_" + k) != v:
                setattr(self, "_" + k, v)
                changed = True
        if changed:
            self._render()

    def _render(self) -> None:
        st = self.st
        border, bw = st["border"], 1
        if not self.enabled:
            if self.kind in ("primary", "danger"):
                fill, fg = mix(self.bgc, st["fill"], 0.40), mix(self.bgc, WHITE, 0.45)
            else:
                fill, fg = st["fill"], TEXT3
        else:
            if self._down and self._hover:
                fill = st["press"]
            elif self._hover:
                fill = st["hover"]
            else:
                fill = st["fill"]
            fg = self._fg or ((self.hover_fg or st["fg"]) if self._hover else st["fg"])
            if self._focus:
                border, bw = ACCENT_TXT, 2
        ui = self.ui
        bpx = max(1, ui.px(bw))
        key = ("btn", self.wpx, self.hpx, self.rpx, fill, self.bgc, border, bpx)
        img = ui.img(key, lambda: rounded(self.wpx, self.hpx, self.rpx, fill, self.bgc, border, bpx))
        self.config(image=img, fg=fg, cursor="hand2" if self.enabled else "arrow")


class Box(tk.Canvas):
    """Контейнер со скруглёнными углами. Содержимое кладётся в .inner, высота подстраивается сама.

    Все размеры — уже в физических пикселях.
    """

    def __init__(self, master, ui, wpx, fill, outer, border=None, rpx=8, padx=12, pady=10):
        super().__init__(master, width=wpx, height=max(1, 2 * pady), bg=outer, highlightthickness=0, bd=0)
        self.ui, self.wpx, self.fill, self.outer, self.border = ui, wpx, fill, outer, border
        self.rpx, self.padx, self.pady = rpx, padx, pady
        self.hpx = 0
        self._photo = None
        self._img = self.create_image(0, 0, anchor="nw")
        self.inner = tk.Frame(self, bg=fill)
        self.create_window(padx, pady, window=self.inner, anchor="nw", width=wpx - 2 * padx)
        self.inner.bind("<Configure>", self._fit)

    def _fit(self, _e=None) -> None:
        h = self.inner.winfo_reqheight() + 2 * self.pady
        if h != self.hpx:
            self.hpx = h
            self.configure(height=h)
            self._draw()

    def _draw(self) -> None:
        if self.hpx <= 0:
            return
        im = rounded(self.wpx, self.hpx, self.rpx, self.fill, self.outer, self.border, max(1, self.ui.px(1)))
        self._photo = ImageTk.PhotoImage(im, master=self)
        self.itemconfigure(self._img, image=self._photo)

    def recolor(self, fill: str, border: str | None = None) -> None:
        self.fill, self.border = fill, border
        self.inner.configure(bg=fill)
        self._draw()


class Toggle(tk.Label):
    """Переключатель вкл/выкл с поддержкой клавиатуры."""

    def __init__(self, master, ui, command, bg=BG):
        super().__init__(master, bd=0, bg=bg, highlightthickness=0, cursor="hand2", takefocus=1)
        self.ui, self.command, self.bgc = ui, command, bg
        self.on = False
        self._hover = self._focus = False
        self.bind("<Enter>", lambda e: self._set("_hover", True))
        self.bind("<Leave>", lambda e: self._set("_hover", False))
        self.bind("<FocusIn>", lambda e: self._set("_focus", True))
        self.bind("<FocusOut>", lambda e: self._set("_focus", False))
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<space>", lambda e: self.command())
        self.bind("<Return>", lambda e: self.command())
        self._render()

    def set(self, on: bool) -> None:
        if bool(on) != self.on:
            self.on = bool(on)
            self._render()

    def _set(self, name: str, v: bool) -> None:
        if getattr(self, name) != v:
            setattr(self, name, v)
            self._render()

    def _render(self) -> None:
        ui = self.ui
        w, h = ui.px(40), ui.px(22)
        on, hov, foc = self.on, self._hover, self._focus

        def make():
            W, H = w * SS, h * SS
            im = Image.new("RGB", (W, H), self.bgc)
            d = ImageDraw.Draw(im)
            if on:
                track, edge = (ACCENT_H if hov else ACCENT), ACCENT
            else:
                track, edge = (HOVER if hov else RAISED), BORDER
            if foc:
                edge = ACCENT_TXT
            d.rounded_rectangle((0, 0, W - 1, H - 1), radius=H // 2, fill=edge)
            o = (2 if foc else 1) * SS * max(1, ui.px(1))
            d.rounded_rectangle((o, o, W - 1 - o, H - 1 - o), radius=H // 2 - o, fill=track)
            kr = H // 2 - int(4 * ui.k * SS)
            cx, cy = (W - H // 2) if on else H // 2, H // 2
            d.ellipse((cx - kr, cy - kr, cx + kr, cy + kr), fill=WHITE if on else TEXT2)
            return im.resize((w, h), LANCZOS)
        self.config(image=ui.img(("toggle", w, h, on, hov, foc, self.bgc), make))


# ---------------------------------------------------------------- окно

class MainWindow:
    def __init__(self, app, m, start_hidden: bool = False):
        self.app = app
        self.m = m
        self.q: queue.Queue = queue.Queue()
        self._settings = None
        self._show_full = False
        self._told_tray = False
        self._checking = False
        self._copied_until = 0.0
        self._last_phase = None
        self._ticks = 0
        self._placed = False
        self._pop = None
        self._photos: dict = {}
        self._imgs: dict = {}
        self._fonts: dict = {}
        self._frames: list = []
        self._frame_i = 0
        self.root = tk.Tk()
        self.root.withdraw()
        self.k = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        self.anim = _animations_enabled()
        self._init_fonts()
        r = self.root
        r.title(m.APP_NAME)
        r.configure(bg=BG)
        r.resizable(False, False)
        r.protocol("WM_DELETE_WINDOW", self.hide)
        self._ttk_dark()
        self._build()
        self._set_icon("connecting")
        self._apply_phase("connecting")
        r.bind("<Configure>", self._on_root_configure)
        dark_titlebar(r)
        if not start_hidden:
            self._center()
            r.deiconify()
        r.after(150, self._tick)
        r.after(90, self._animate)

    # --- базовое
    def px(self, v: float) -> int:
        return int(round(v * self.k))

    def img(self, key, make):
        p = self._imgs.get(key)
        if p is None:
            p = ImageTk.PhotoImage(make(), master=self.root)
            self._imgs[key] = p
        return p

    def measure(self, font, text: str) -> int:
        fo = self._fonts.get(font)
        if fo is None:
            fo = tkfont.Font(root=self.root, font=font)
            self._fonts[font] = fo
        return fo.measure(text)

    def _init_fonts(self) -> None:
        fams = {x.lower() for x in tkfont.families(self.root)}

        def pick(*names):
            for n in names:
                if n.lower() in fams:
                    return n
            return None
        base = pick("Segoe UI", "Arial") or "Arial"
        semi = pick("Segoe UI Semibold")
        mono = pick("Cascadia Mono", "Consolas", "Courier New") or "Courier New"

        def strong(size):
            return (semi, size) if semi else (base, size, "bold")
        self.F_BODY = (base, 10)
        self.F_SMALL = (base, 9)
        self.F_LABEL = strong(10)
        self.F_BTN = strong(10)
        self.F_NUM = strong(9)
        self.F_TITLE = strong(14)
        self.F_HERO = strong(17)
        self.F_MONO = (mono, 10)

    def _ttk_dark(self) -> None:
        """Тёмная тема для стандартных ttk-элементов (окно настроек)."""
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            return
        st.configure(".", background=BG, foreground=TEXT, fieldbackground=RAISED, bordercolor=BORDER,
                     lightcolor=RAISED, darkcolor=RAISED, troughcolor=SURFACE, focuscolor=ACCENT_TXT,
                     selectbackground=ACCENT, selectforeground=WHITE, insertcolor=TEXT, font=self.F_BODY)
        st.map(".", foreground=[("disabled", TEXT3)])
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=TEXT)
        st.configure("Hint.TLabel", background=BG, foreground=TEXT3)
        st.configure("Err.TLabel", background=BG, foreground=RED)
        st.configure("TSeparator", background=BORDER)
        st.configure("TButton", background=RAISED, foreground=TEXT, bordercolor=BORDER, lightcolor=RAISED,
                     darkcolor=RAISED, padding=(14, 6), font=self.F_BTN, focuscolor=RAISED)
        st.map("TButton",
               background=[("pressed", PRESSED), ("active", HOVER)],
               lightcolor=[("pressed", PRESSED), ("active", HOVER)],
               darkcolor=[("pressed", PRESSED), ("active", HOVER)],
               bordercolor=[("focus", ACCENT_TXT)])
        st.configure("TEntry", fieldbackground=RAISED, foreground=TEXT, bordercolor=BORDER, lightcolor=RAISED,
                     darkcolor=RAISED, insertcolor=TEXT, padding=(8, 4))
        st.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
        st.configure("Accent.TButton", background=ACCENT, foreground=WHITE, bordercolor=ACCENT,
                     lightcolor=ACCENT, darkcolor=ACCENT)
        st.map("Accent.TButton", background=[("pressed", ACCENT_P), ("active", ACCENT_H)],
               lightcolor=[("pressed", ACCENT_P), ("active", ACCENT_H)],
               darkcolor=[("pressed", ACCENT_P), ("active", ACCENT_H)],
               foreground=[("disabled", TEXT3)], bordercolor=[("focus", ACCENT_TXT)])
        for cls in ("TRadiobutton", "TCheckbutton"):
            st.configure(cls, background=BG, foreground=TEXT, indicatorbackground=RAISED,
                         indicatorforeground=WHITE, upperbordercolor=BORDER, lowerbordercolor=BORDER,
                         focuscolor=BG, padding=(2, 3))
            st.map(cls, background=[("active", BG)],
                   indicatorbackground=[("selected", ACCENT), ("active", HOVER)],
                   upperbordercolor=[("selected", ACCENT)], lowerbordercolor=[("selected", ACCENT)])

    # --- разметка
    def _label(self, parent, text, font=None, fg=TEXT, bg=BG, **kw):
        return tk.Label(parent, text=text, font=font or self.F_BODY, fg=fg, bg=bg, bd=0, **kw)

    def _build(self) -> None:
        m, px = self.m, self.px
        W = px(CONTENT_W)
        f = tk.Frame(self.root, bg=BG, padx=px(GUTTER), pady=px(20))
        f.pack(fill="both", expand=True)

        # шапка
        head = tk.Frame(f, bg=BG)
        head.pack(fill="x")
        self.head_icon = tk.Label(head, bg=BG, bd=0)
        self.head_icon.pack(side="left", padx=(0, px(12)))
        tt = tk.Frame(head, bg=BG)
        tt.pack(side="left", fill="x", expand=True)
        self._label(tt, m.APP_NAME, self.F_TITLE).pack(anchor="w")
        self._label(tt, "Подключает Notion к этому компьютеру по ссылке", self.F_SMALL, TEXT2).pack(anchor="w")
        self._label(head, "v" + m.VERSION, self.F_SMALL, TEXT3).pack(side="right", anchor="n")

        # статус — главный элемент окна
        self.card = Box(f, self, W, SURFACE, BG, BORDER, rpx=px(12), padx=px(18), pady=px(14))
        self.card.pack(anchor="w", pady=(px(18), 0))
        ci = self.card.inner
        self.beacon = tk.Label(ci, bd=0, bg=SURFACE)
        self.beacon.pack(side="left", padx=(0, px(14)))
        self.st_box = tk.Frame(ci, bg=SURFACE)
        self.st_box.pack(side="left", fill="x", expand=True)
        self.st_title = self._label(self.st_box, "Запускаю…", self.F_HERO, TEXT, SURFACE, anchor="w")
        self.st_title.pack(anchor="w")
        self.st_detail = self._label(self.st_box, "", self.F_BODY, TEXT2, SURFACE, anchor="w", justify="left",
                                     wraplength=W - px(18 * 2 + 40 + 14) - 4)
        self.st_detail.pack(anchor="w")

        # ссылка
        self._label(f, "Ссылка для Notion", self.F_LABEL).pack(anchor="w", pady=(px(20), 0))
        fld = Box(f, self, W, RAISED, BG, BORDER, rpx=px(8), padx=px(12), pady=px(5))
        fld.pack(anchor="w", pady=(px(8), 0))
        fi = fld.inner
        self.link_var = tk.StringVar(value="")
        self.link_entry = tk.Entry(fi, textvariable=self.link_var, state="readonly", readonlybackground=RAISED,
                                   bg=RAISED, fg=TEXT, relief="flat", bd=0, highlightthickness=0,
                                   font=self.F_MONO, selectbackground=ACCENT, selectforeground=WHITE,
                                   insertbackground=TEXT)
        self.link_entry.pack(side="left", fill="x", expand=True)
        self.eye_btn = Btn(fi, self, "Показать", self.toggle_full, "ghost", width=84, height=28, bg=RAISED,
                           font=self.F_SMALL, radius=6)
        self.eye_btn.pack(side="right", padx=(px(8), 0))
        self.btn_row = tk.Frame(f, bg=BG)
        self.btn_row.pack(fill="x", pady=(px(12), 0))
        self.copy_btn = Btn(self.btn_row, self, "Скопировать ссылку", self.copy, "primary", width=236, height=40)
        self.copy_btn.pack(side="left")
        self.check_btn = Btn(self.btn_row, self, "Проверить ссылку", self.check, "secondary", width=180, height=40)
        self.check_btn.pack(side="left", padx=(px(8), 0))
        self.check_lbl = self._label(f, "", self.F_SMALL, TEXT2, anchor="w", justify="left", wraplength=W)

        # шаги — это последовательность, поэтому с номерами
        self.how = tk.Frame(f, bg=BG)
        self.how.pack(fill="x", pady=(px(20), 0))
        self.how_lbl = self._label(self.how, "Как подключить", self.F_LABEL)
        self.how_lbl.pack(anchor="w")
        steps = (
            "Нажми «Скопировать ссылку», когда статус станет «Работает».",
            "В Notion добавь свой MCP-сервер и вставь эту ссылку как его адрес.",
            "В Authentication выбери «None». Если такого пункта нет — выбери Bearer token "
            "и впиши в Token что угодно (хоть эту же ссылку).",
            "Не выключай программу. Крестик просто прячет её в трей.",
        )
        d = px(22)
        badge = self.img(("badge", d), lambda: rounded(d, d, d // 2, RAISED, BG))
        for i, s in enumerate(steps, 1):
            row = tk.Frame(self.how, bg=BG)
            row.pack(fill="x", pady=(px(7), 0))
            tk.Label(row, image=badge, text=str(i), compound="center", font=self.F_NUM, fg=TEXT2, bg=BG,
                     bd=0).pack(side="left", anchor="n")
            self._label(row, s, self.F_BODY, TEXT2, anchor="w", justify="left",
                        wraplength=W - d - px(12)).pack(side="left", anchor="n", padx=(px(12), 0), pady=(px(1), 0))

        # способ связи
        mr = tk.Frame(f, bg=BG)
        mr.pack(fill="x", pady=(px(20), 0))
        self._label(mr, "Способ связи", self.F_LABEL).pack(side="left")
        self.settings_btn = Btn(mr, self, "Настройки", self.open_settings, "secondary", height=36)
        self.settings_btn.pack(side="right")
        ls = self._fonts[self.F_BODY].metrics("linespace") if self.F_BODY in self._fonts else self._linespace()
        dw = W - self.measure(self.F_LABEL, "Способ связи") - px(14) - self.settings_btn.wpx - px(8)
        self.dd = Box(mr, self, dw, RAISED, BG, BORDER, rpx=px(8), padx=px(12), pady=max(2, (px(36) - ls) // 2))
        self.dd.pack(side="right", padx=(0, px(8)))
        self.dd_text = self._label(self.dd.inner, "", self.F_BODY, TEXT, RAISED, anchor="w")
        self.dd_text.pack(side="left")
        self.dd_chev = self._label(self.dd.inner, "▾", self.F_BODY, TEXT2, RAISED)
        self.dd_chev.pack(side="right")
        self._dd_parts = (self.dd, self.dd.inner, self.dd_text, self.dd_chev)
        for w in self._dd_parts:
            w.configure(cursor="hand2")
            w.bind("<Button-1>", lambda e: self.toggle_modes())
            w.bind("<Enter>", lambda e: self._dd_hover())
            w.bind("<Leave>", lambda e: self.root.after(30, self._dd_hover))

        # автозапуск
        ar = tk.Frame(f, bg=BG)
        ar.pack(fill="x", pady=(px(16), 0))
        self.toggle = Toggle(ar, self, self._on_autostart, BG)
        self.toggle.pack(side="left", anchor="n", pady=(px(2), 0))
        at = tk.Frame(ar, bg=BG)
        at.pack(side="left", padx=(px(12), 0))
        l1 = self._label(at, "Запускать вместе с Windows")
        l1.pack(anchor="w")
        l2 = self._label(at, "При автозапуске окно не открывается, значок сразу появится возле часов.",
                         self.F_SMALL, TEXT3)
        l2.pack(anchor="w")
        for w in (l1, l2):
            w.configure(cursor="hand2")
            w.bind("<Button-1>", lambda e: self._on_autostart())

        # низ
        tk.Frame(f, bg=BORDER, height=max(1, px(1))).pack(fill="x", pady=(px(18), px(12)))
        bot = tk.Frame(f, bg=BG)
        bot.pack(fill="x")
        Btn(bot, self, "Журнал", lambda: m.open_path(m.LOG_FILE), "ghost", height=34,
            font=self.F_BODY).pack(side="left")
        Btn(bot, self, "Новая ссылка…", self.regenerate, "ghost", height=34,
            font=self.F_BODY).pack(side="left", padx=(px(2), 0))
        Btn(bot, self, "Выход", self.ask_quit, "ghost", height=34, font=self.F_BODY,
            hover_fg=RED).pack(side="right")
        Btn(bot, self, "Свернуть в трей", self.hide, "secondary", height=34).pack(side="right", padx=(0, px(6)))

    def _linespace(self) -> int:
        self.measure(self.F_BODY, "x")
        return self._fonts[self.F_BODY].metrics("linespace")

    def _set_icon(self, ph: str) -> None:
        try:
            if ph not in self._photos:
                self._photos[ph] = (
                    ImageTk.PhotoImage(self.m.make_image(ph, 64), master=self.root),
                    ImageTk.PhotoImage(self.m.make_image(ph, self.px(40)), master=self.root),
                )
            win_img, head_img = self._photos[ph]
            self.root.iconphoto(True, win_img)
            self.head_icon.config(image=head_img)
        except Exception:  # noqa: BLE001
            self.m.log.exception("Иконка окна")

    def _beacon_frames(self, ph: str, fill: str) -> list:
        """Точка статуса. Пока идёт подключение — мягкая «волна», в остальных состояниях статично."""
        S = self.px(40)
        c, bgc = _rgb(PHASES[ph][1]), _rgb(fill)
        pulse = ph == "connecting" and self.anim
        n, wave = (22, 15) if pulse else (1, 0)
        frames = []
        for i in range(n):
            W = S * SS
            im = Image.new("RGB", (W, W), fill)
            d = ImageDraw.Draw(im)
            cx = W / 2
            if pulse:
                t = i / wave
                rr, a = (7 + 12 * t) * self.k * SS, (0.5 * (1 - t) if i < wave else 0.0)
            else:
                rr, a = 13 * self.k * SS, 0.20
            if a > 0:
                col = tuple(round(b + (x - b) * a) for x, b in zip(c, bgc))
                d.ellipse((cx - rr, cx - rr, cx + rr, cx + rr), fill=col)
            r0 = 7 * self.k * SS
            d.ellipse((cx - r0, cx - r0, cx + r0, cx + r0), fill=c)
            frames.append(ImageTk.PhotoImage(im.resize((S, S), LANCZOS), master=self.root))
        return frames

    def _apply_phase(self, ph: str) -> None:
        col = PHASES[ph][1]
        fill, border = mix(SURFACE, col, 0.08), mix(SURFACE, col, 0.34)
        self.card.recolor(fill, border)
        for w in (self.st_box, self.st_title, self.st_detail, self.beacon):
            w.config(bg=fill)
        key = ("beacon", ph, fill, self.anim)
        if key not in self._photos:
            self._photos[key] = self._beacon_frames(ph, fill)
        self._frames = self._photos[key]
        self._frame_i = 0
        self.beacon.config(image=self._frames[0])

    def _animate(self) -> None:
        try:
            if len(self._frames) > 1 and self.root.state() != "withdrawn":
                self._frame_i = (self._frame_i + 1) % len(self._frames)
                self.beacon.config(image=self._frames[self._frame_i])
            self.root.after(70, self._animate)
        except tk.TclError:
            pass

    def _center(self) -> None:
        r = self.root
        r.update_idletasks()
        w, h = r.winfo_reqwidth(), r.winfo_reqheight()
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        if h > sh - self.px(90) and self.how.winfo_manager():
            # маленький экран (ноутбук с масштабом 150%): шаги есть в README, прячем их
            self.how.pack_forget()
            r.update_idletasks()
            w, h = r.winfo_reqwidth(), r.winfo_reqheight()
        x = max(0, (sw - w) // 2)
        y = max(0, min((sh - h) // 3, sh - h - self.px(72)))
        r.geometry(f"+{x}+{y}")
        self._placed = True

    # --- цикл обновления (всё, что трогает окно, выполняется только здесь, в потоке Tk)
    def call(self, fn) -> None:
        self.q.put(fn)

    def _tick(self) -> None:
        while True:
            try:
                fn = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:  # noqa: BLE001
                self.m.log.exception("Окно")
        try:
            self._update()
        except Exception:  # noqa: BLE001
            self.m.log.exception("Обновление окна")
        try:
            self.root.after(500, self._tick)
        except tk.TclError:
            pass

    def _short(self, k: str) -> str:
        return self.m.MODES.get(k, k).split(" (")[0]

    def _update(self) -> None:
        app, m = self.app, self.m
        self._ticks += 1
        self._watch_pop()
        ph = app.phase()
        st = app.status_text()
        detail = st[2:] if len(st) > 2 and st[1] == " " else st
        if ph == "connecting" and not app.server.ready:
            title = "Запускаю…"
        else:
            title = PHASES[ph][0]
        if ph == "online":
            detail = detail + m.NL + "Ссылку можно вставлять в Notion."
        if ph != self._last_phase:
            self._last_phase = ph
            self._apply_phase(ph)
            self._set_icon(ph)
        if self.st_title.cget("text") != title:
            self.st_title.config(text=title)
            self.root.title(m.APP_NAME + " — " + title)
        if self.st_detail.cget("text") != detail:
            self.st_detail.config(text=detail)
        link = app.link()
        if link:
            shown = link if self._show_full else m.mask_link(link, app.cfg.get("token") or "")
        else:
            shown = "появится, когда будет связь…"
        self.copy_btn.set_enabled(bool(link))
        self.check_btn.set_enabled(bool(link) and not self._checking)
        self.eye_btn.set_enabled(bool(link))
        self.link_entry.config(fg=TEXT if link else TEXT3)
        if self.link_var.get() != shown:
            self.link_var.set(shown)
        if self._copied_until and time.time() > self._copied_until:
            self._copied_until = 0.0
            self.copy_btn.set_text("Скопировать ссылку")
        cur = self._short(app.cfg["mode"])
        if self.dd_text.cget("text") != cur:
            self.dd_text.config(text=cur)
        if self._ticks % 10 == 1:
            self.toggle.set(m.autostart_enabled())

    # --- выпадающий список способов связи
    def _dd_hover(self) -> None:
        try:
            x, y = self.root.winfo_pointerxy()
            inside = self.root.winfo_containing(x, y) in self._dd_parts
        except (tk.TclError, KeyError):
            inside = False
        fill = HOVER if inside or self._pop is not None else RAISED
        if self.dd.fill != fill:
            self.dd.recolor(fill, ACCENT if self._pop is not None else BORDER)
            for w in (self.dd_text, self.dd_chev):
                w.config(bg=fill)

    def toggle_modes(self) -> None:
        if self._pop is not None:
            self._close_pop()
            return
        m, px = self.m, self.px
        pop = tk.Toplevel(self.root)
        pop.withdraw()
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.configure(bg=BORDER)
        inner = tk.Frame(pop, bg=SURFACE)
        inner.pack(padx=max(1, px(1)), pady=max(1, px(1)))
        cur = self.app.cfg["mode"]
        for k, title in m.MODES.items():
            row = tk.Frame(inner, bg=SURFACE, cursor="hand2")
            row.pack(fill="x")
            mark = self._label(row, "✓" if k == cur else "", self.F_BODY, ACCENT_TXT, SURFACE, width=2)
            mark.pack(side="left", padx=(px(10), 0), pady=px(8))
            lab = self._label(row, title, self.F_BODY, TEXT, SURFACE, anchor="w")
            lab.pack(side="left", fill="x", expand=True, padx=(px(4), px(18)), pady=px(8))
            parts = (row, mark, lab)
            for w in parts:
                w.configure(cursor="hand2")
                w.bind("<Enter>", lambda e, ps=parts: [p.config(bg=HOVER) for p in ps])
                w.bind("<Leave>", lambda e, ps=parts: [p.config(bg=SURFACE) for p in ps])
                w.bind("<Button-1>", lambda e, key=k: self._pick_mode(key))
        pop.update_idletasks()
        x = self.dd.winfo_rootx()
        y = self.dd.winfo_rooty() + self.dd.winfo_height() + px(4)
        w = max(self.dd.winfo_width(), pop.winfo_reqwidth())
        x = min(x, self.root.winfo_rootx() + self.root.winfo_width() - w - px(8))
        pop.geometry(f"{w}x{pop.winfo_reqheight()}+{x}+{y}")
        pop.deiconify()
        pop.lift()
        pop.bind("<Escape>", lambda e: self._close_pop())
        if not getattr(self, "_bound_all", False):
            self._bound_all = True
            self.root.bind_all("<Button-1>", self._outside_click, add="+")
            self.root.bind("<Escape>", lambda e: self._close_pop(), add="+")
        pop.focus_force()
        self._pop = pop
        self._pop_focus = False
        self._pop_anchor = (self.root.winfo_rootx(), self.root.winfo_rooty())
        self._dd_hover()

    def _on_root_configure(self, e) -> None:
        # закрываем список, только если окно действительно сдвинули
        if e.widget is not self.root or self._pop is None:
            return
        pos = (self.root.winfo_rootx(), self.root.winfo_rooty())
        if pos != getattr(self, "_pop_anchor", pos):
            self._close_pop()

    def _outside_click(self, e) -> None:
        p = self._pop
        if p is None:
            return
        w = e.widget
        if w in self._dd_parts:
            return  # сама кнопка списка работает как переключатель
        if not str(w).startswith(str(p)):
            self._close_pop()

    def _watch_pop(self) -> None:
        """Закрывает список, если пользователь ушёл в другую программу."""
        if self._pop is None:
            return
        try:
            focused = self.root.focus_get() is not None
        except (tk.TclError, KeyError):
            focused = True
        if focused:
            self._pop_focus = True
        elif self._pop_focus:
            self._close_pop()

    def _close_pop(self) -> None:
        p, self._pop = self._pop, None
        if p is not None:
            try:
                p.destroy()
            except tk.TclError:
                pass
            self.dd.fill = None
            self._dd_hover()

    def _pick_mode(self, k: str) -> None:
        self._close_pop()
        self.select_mode(k)

    # --- действия
    def show(self) -> None:
        r = self.root
        if not self._placed:
            self._center()
        r.deiconify()
        r.lift()
        r.attributes("-topmost", True)
        r.after(400, lambda: r.attributes("-topmost", False))
        try:
            r.focus_force()
        except tk.TclError:
            pass

    def hide(self) -> None:
        self._close_pop()
        self.root.withdraw()
        if not self._told_tray:
            self._told_tray = True
            self.app.notify("Программа работает в трее возле часов. Нажми на значок, чтобы открыть окно.")

    def toggle_full(self) -> None:
        self._show_full = not self._show_full
        self.eye_btn.set_text("Скрыть" if self._show_full else "Показать")
        self._update()

    def copy(self) -> None:
        if not self.app.link():
            return
        self.app.copy_link(silent=True)
        self._copied_until = time.time() + 2.5
        self.copy_btn.set_text("✓  Скопировано")

    def _show_check(self, text: str, color: str) -> None:
        self.check_lbl.config(text=text, fg=color)
        if text:
            if not self.check_lbl.winfo_ismapped():
                self.check_lbl.pack(after=self.btn_row, anchor="w", pady=(self.px(8), 0))
        else:
            self.check_lbl.pack_forget()

    def check(self) -> None:
        if self._checking or not self.app.link():
            return
        self._checking = True
        self.check_btn.set_enabled(False)
        self._show_check("Проверяю ссылку так, как её откроет Notion…", TEXT2)

        def run() -> None:
            try:
                ok, msg = self.app.check_link()
            except Exception as e:  # noqa: BLE001
                ok, msg = False, self.m.short_err(e)

            def done() -> None:
                self._checking = False
                self.check_btn.set_enabled(True)
                self._show_check(("✓  " if ok else "✗  ") + msg, GREEN if ok else RED)
            self.call(done)
        threading.Thread(target=run, name="check", daemon=True).start()

    def select_mode(self, k: str) -> None:
        if k == self.app.cfg["mode"]:
            return
        self._show_check("", TEXT2)
        self.app.set_mode(k)
        self.dd_text.config(text=self._short(k))
        if k == "own" and not (self.app.cfg.get("own_relay_url") or "").strip():
            self.open_settings()

    def _on_autostart(self) -> None:
        self.app.toggle_autostart()
        self.toggle.set(self.m.autostart_enabled())

    def open_settings(self) -> None:
        s = self._settings
        try:
            if s is not None and s.winfo_exists():
                s.deiconify()
                s.lift()
                s.focus_force()
                return
        except tk.TclError:
            pass
        self.line_color = BORDER
        self._settings = self.app._settings_window(parent=self.root)
        s = self._settings
        if s is not None:
            try:
                s.configure(bg=BG)
                dark_titlebar(s)
            except tk.TclError:
                pass

    def regenerate(self) -> None:
        threading.Thread(target=self.app.regenerate, name="regenerate", daemon=True).start()

    def confirm(self, title: str, text: str, ok_text: str) -> bool:
        """Тёмный диалог подтверждения вместо стандартного светлого."""
        px = self.px
        d = tk.Toplevel(self.root)
        d.withdraw()
        d.title(self.m.APP_NAME)
        d.configure(bg=BG)
        d.resizable(False, False)
        d.transient(self.root)
        fr = tk.Frame(d, bg=BG, padx=px(24), pady=px(22))
        fr.pack()
        self._label(fr, title, self.F_TITLE).pack(anchor="w")
        self._label(fr, text, self.F_BODY, TEXT2, justify="left", wraplength=px(360)).pack(
            anchor="w", pady=(px(8), px(22)))
        res = {"ok": False}

        def ok() -> None:
            res["ok"] = True
            d.destroy()
        btns = tk.Frame(fr, bg=BG)
        btns.pack(fill="x")
        cancel = Btn(btns, self, "Отмена", d.destroy, "secondary", height=36)
        cancel.pack(side="right")
        Btn(btns, self, ok_text, ok, "danger", height=36).pack(side="right", padx=(0, px(8)))
        d.bind("<Escape>", lambda e: d.destroy())
        dark_titlebar(d)
        d.update_idletasks()
        r = self.root
        x = r.winfo_rootx() + (r.winfo_width() - d.winfo_reqwidth()) // 2
        y = r.winfo_rooty() + (r.winfo_height() - d.winfo_reqheight()) // 3
        d.geometry(f"+{max(0, x)}+{max(0, y)}")
        d.deiconify()
        try:
            d.grab_set()
        except tk.TclError:
            pass
        cancel.focus_set()
        r.wait_window(d)
        return res["ok"]

    def ask_quit(self) -> None:
        if self.confirm("Выйти из программы?",
                        "Пока она выключена, Notion не сможет подключиться к этому компьютеру. "
                        "Чтобы просто убрать окно, нажми «Свернуть в трей».",
                        "Выйти"):
            self.quit_all()

    def quit_all(self) -> None:
        self._close_pop()
        self.root.withdraw()
        self.root.quit()

    def close(self) -> None:
        self.call(self.root.quit)

    def listen_show(self) -> None:
        if os.name != "nt":
            return
        try:
            k32 = _k32()
            h = k32.CreateEventW(None, 0, 0, _event_name(self.m))
            if not h:
                return
            while True:
                if k32.WaitForSingleObject(h, 0xFFFFFFFF) == 0:
                    self.call(self.show)
                else:
                    time.sleep(1)
        except Exception:  # noqa: BLE001
            self.m.log.exception("Ожидание сигнала «показать окно»")


# ---------------------------------------------------------------- трей

def make_tray(app, m, win: MainWindow):
    import pystray
    from pystray import Menu
    from pystray import MenuItem as Item

    def noop(icon, item) -> None:
        pass

    def mode_item(k: str):
        return Item(m.MODES[k], lambda icon, item: win.call(lambda: win.select_mode(k)),
                    checked=lambda item: app.cfg["mode"] == k, radio=True)

    menu = Menu(
        Item("Открыть окно", lambda icon, item: win.call(win.show), default=True),
        Item(lambda item: app.status_text()[:100], noop, enabled=False),
        Menu.SEPARATOR,
        Item("Скопировать ссылку для Notion", lambda icon, item: app.copy_link()),
        Item("Проверить ссылку", lambda icon, item: app.check_async()),
        Menu.SEPARATOR,
        Item("Способ связи", Menu(*[mode_item(k) for k in m.MODES])),
        Item("Настройки…", lambda icon, item: win.call(win.open_settings)),
        Item("Запускать вместе с Windows", lambda icon, item: win.call(win._on_autostart),
             checked=lambda item: m.autostart_enabled()),
        Item("Открыть журнал", lambda icon, item: m.open_path(m.LOG_FILE)),
        Menu.SEPARATOR,
        Item("Выход", lambda icon, item: win.call(win.quit_all)),
    )
    return pystray.Icon("PcMcpConnect", m.make_image("connecting"), m.APP_NAME, menu)


# ---------------------------------------------------------------- запуск

def run(app, m, start_hidden: bool = False) -> None:
    _dpi_aware()
    win = MainWindow(app, m, start_hidden=start_hidden)
    app.window = win
    try:
        icon = make_tray(app, m, win)

        def setup(ic) -> None:
            ic.visible = True
        icon.run_detached(setup=setup)
        app.icon = icon
    except Exception:  # noqa: BLE001
        m.log.exception("Значок в трее не запустился")
        app.icon = None
        if start_hidden:
            win.show()
    threading.Thread(target=win.listen_show, name="show-signal", daemon=True).start()
    threading.Thread(target=app.start, name="start", daemon=True).start()
    try:
        win.root.mainloop()
    finally:
        app.window = None
        try:
            win.root.destroy()
        except tk.TclError:
            pass
        app.quit()
