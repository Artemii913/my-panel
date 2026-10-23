"""Маркер поверх экрана — Alt+S.

Alt+S          — рисовать (курсор-крестик, по краю монитора жёлтая рамка)
левая кнопка   — жёлтый маркер, правая — красная ручка, Ctrl+Z — отменить штрих
Alt+S / Enter  — закончить: пометки остаются, а мышь снова работает с окнами под ними
Пометки стираются сами при прокрутке (колесо, PgUp/PgDn, стрелки), переходе в другое окно или по Esc.

Отдельный процесс: у Tk свой главный поток, у панели (pywebview) — свой.
"""
import ctypes
import ctypes.wintypes as wt
import queue
import threading
import tkinter as tk

ctypes.windll.shcore.SetProcessDpiAwareness(2)  # физические пиксели на обоих мониторах
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

HOTKEY_MOD, HOTKEY_VK = 0x0001 | 0x4000, 0x53   # Alt+S
KEY = "#ff00fe"                  # цвет-«дырка»: всё этого цвета прозрачно и пропускает клики
INK_ALPHA = 0.5
MARKER = dict(fill="#ffe24a", width=24)
PEN = dict(fill="#ff4d5e", width=5)

user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.MonitorFromPoint.restype = wt.HMONITOR
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.GetParent.restype = wt.HWND
user32.GetParent.argtypes = [wt.HWND]
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.SetWindowsHookExW.restype = wt.HHOOK

events: "queue.Queue[str]" = queue.Queue()
state = {"mode": "idle"}          # idle | draw | marks — читают хуки из другого потока


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def monitor_under_cursor():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromPoint(pt, 2), ctypes.byref(info))
    r = info.rcMonitor
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def hwnd_of(win: tk.Toplevel):
    win.update_idletasks()
    return user32.GetParent(win.winfo_id()) or win.winfo_id()


def add_exstyle(hwnd, bits, remove=0):
    ex = user32.GetWindowLongPtrW(hwnd, -20)
    user32.SetWindowLongPtrW(hwnd, -20, (ex | bits) & ~remove)


def place(hwnd, x, y, w, h):
    user32.SetWindowPos(hwnd, wt.HWND(-1), x, y, w, h, 0x0010)  # TOPMOST, NOACTIVATE


# ---------- горячая клавиша и глобальные хуки (свой поток со своим циклом сообщений) ----------

LowLevelProc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
SCROLL_KEYS = {0x21, 0x22, 0x23, 0x24, 0x26, 0x28}  # PgUp PgDn End Home ↑ ↓
WM_MOUSEWHEEL, WM_MOUSEHWHEEL, WM_KEYDOWN, WM_HOTKEY = 0x020A, 0x020E, 0x0100, 0x0312


class KBD(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


def _mouse_hook(code, wparam, lparam):
    if code == 0 and wparam in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL) and state["mode"] != "idle":
        events.put("clear")
    return user32.CallNextHookEx(None, code, wparam, lparam)


def _kbd_hook(code, wparam, lparam):
    if code == 0 and wparam == WM_KEYDOWN and state["mode"] != "idle":
        vk = ctypes.cast(lparam, ctypes.POINTER(KBD)).contents.vkCode
        if vk == 0x1B:
            events.put("clear")
        elif vk in SCROLL_KEYS and state["mode"] == "marks":
            events.put("clear")
    return user32.CallNextHookEx(None, code, wparam, lparam)


_procs = (LowLevelProc(_mouse_hook), LowLevelProc(_kbd_hook))  # держим ссылки, иначе GC


def input_thread():
    if not user32.RegisterHotKey(None, 1, HOTKEY_MOD, HOTKEY_VK):
        events.put("hotkey-busy")
        return
    user32.SetWindowsHookExW(14, _procs[0], None, 0)  # WH_MOUSE_LL
    user32.SetWindowsHookExW(13, _procs[1], None, 0)  # WH_KEYBOARD_LL
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY:
            events.put("toggle")


# ---------- окна ----------

class Overlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        # холст с пометками: прозрачный по цвету KEY, полупрозрачный целиком, клики насквозь
        self.ink = tk.Toplevel(self.root)
        self.ink.overrideredirect(True)
        self.ink.attributes("-topmost", True, "-transparentcolor", KEY, "-alpha", INK_ALPHA)
        self.canvas = tk.Canvas(self.ink, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.ink.withdraw()
        # «ловушка» мыши на время рисования: почти невидимая, но принимает клики
        self.catcher = tk.Toplevel(self.root)
        self.catcher.overrideredirect(True)
        self.catcher.attributes("-topmost", True, "-alpha", 0.01)
        self.catcher.configure(bg="black", cursor="crosshair")
        self.catcher.withdraw()
        for btn, style in (("1", MARKER), ("3", PEN)):
            self.catcher.bind(f"<ButtonPress-{btn}>", lambda e, s=style: self.start(e, s))
            self.catcher.bind(f"<B{btn}-Motion>", self.extend)
        self.catcher.bind("<Control-z>", lambda e: self.undo())
        self.catcher.bind("<Control-Z>", lambda e: self.undo())
        self.catcher.bind("<Return>", lambda e: self.finish())

        self.ink_hwnd = hwnd_of(self.ink)
        self.catcher_hwnd = hwnd_of(self.catcher)
        add_exstyle(self.ink_hwnd, 0x20 | 0x80 | 0x08000000, remove=0x40000)  # TRANSPARENT|TOOLWINDOW|NOACTIVATE
        add_exstyle(self.catcher_hwnd, 0x80, remove=0x40000)                  # без кнопки на панели задач

        self.strokes, self.line, self.points = [], None, []
        self.geom = None
        self.fg_before = None
        self.watch_fg = None
        self.grace = 0
        self.root.after(30, self.pump)

    # --- переходы ---
    def toggle(self):
        mode = state["mode"]
        if mode == "draw":
            self.finish()
        else:
            self.begin(keep=(mode == "marks"))

    def begin(self, keep=False):
        if not keep:
            self.fg_before = user32.GetForegroundWindow()
            self.geom = monitor_under_cursor()
        x, y, w, h = self.geom
        self.canvas.config(width=w, height=h)
        self.canvas.delete("frame")
        self.canvas.create_rectangle(2, 2, w - 3, h - 3, outline=MARKER["fill"], width=4, tags="frame")
        for win, hwnd in ((self.ink, self.ink_hwnd), (self.catcher, self.catcher_hwnd)):
            win.deiconify()
            place(hwnd, x, y, w, h)
        user32.SetForegroundWindow(self.catcher_hwnd)
        self.catcher.focus_force()
        state["mode"] = "draw"

    def finish(self):
        self.canvas.delete("frame")
        self.catcher.withdraw()
        if not self.strokes:
            self.clear()
            return
        state["mode"] = "marks"
        if self.fg_before:
            user32.SetForegroundWindow(self.fg_before)
        self.watch_fg = None
        self.grace = 8  # ~0.4 с: даём фокусу вернуться, прежде чем следить за сменой окна

    def clear(self):
        self.canvas.delete("all")
        self.strokes.clear()
        self.catcher.withdraw()
        self.ink.withdraw()
        if state["mode"] == "draw" and self.fg_before:
            user32.SetForegroundWindow(self.fg_before)
        state["mode"] = "idle"

    # --- рисование ---
    def start(self, e, style):
        self.points = [e.x, e.y, e.x + 0.1, e.y]
        self.line = self.canvas.create_line(*self.points, capstyle="round", joinstyle="round", smooth=True, **style)
        self.strokes.append(self.line)

    def extend(self, e):
        if self.line is None:
            return
        self.points += [e.x, e.y]
        self.canvas.coords(self.line, *self.points)

    def undo(self):
        if self.strokes:
            self.canvas.delete(self.strokes.pop())

    # --- события из потока хуков + слежение за окном ---
    def pump(self):
        try:
            while True:
                ev = events.get_nowait()
                if ev == "toggle":
                    self.toggle()
                elif ev == "clear":
                    self.clear()
                elif ev == "hotkey-busy":
                    user32.MessageBoxW(None, "Alt+S уже занята другой программой. Поменяй HOTKEY_VK в overlay.py.",
                                       "Маркер поверх экрана", 0x30)
        except queue.Empty:
            pass
        if state["mode"] == "marks":
            fg = user32.GetForegroundWindow()
            if self.grace > 0:
                self.grace -= 1
                self.watch_fg = fg
            elif fg and fg != self.watch_fg and fg not in (self.ink_hwnd, self.catcher_hwnd):
                self.clear()   # ушёл в другое окно — пометки больше не про то
        self.root.after(50, self.pump)


def main():
    kernel32.CreateMutexW(None, False, "Local\\MyPanel.Overlay")
    if kernel32.GetLastError() == 183:
        return
    threading.Thread(target=input_thread, daemon=True).start()
    Overlay().root.mainloop()


if __name__ == "__main__":
    main()
