"""Полка для файлов — приёмная часть.

Тянешь файл или папку к верхнему краю экрана — появляется «Положить на полку», отпускаешь туда.
Внутри панели (panel.py зовёт start_in_panel) это делает сама панель: шторка расширяется в приёмник,
открытая панель сжимается в полоску, а здесь остаётся только невидимое окно, которое ловит бросок.
Отдельно (python shelf.py) — показывает свою плашку, как раньше.
Сами файлы лежат во вкладке «Полка» в панели (Alt+Пробел): оттуда их перетаскивают в нужное окно.
Реагирует только на файлы и папки: текст, ссылки, вкладки и окна её не будят.

Как плашка узнаёт, что тащат файл: пока зажата кнопка мыши, вдоль верхнего края
экрана лежит невидимая полоска-датчик. Windows при перетаскивании сама сообщает
окну под курсором, что именно несут (DragEnter), — плашка появляется, только если там файлы.

Список хранится в %LOCALAPPDATA%\\MyPanel\\shelf.json (только пути, сами файлы никуда не копируются),
его же читает и правит вкладка «Полка» в панели.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import threading
import time
from pathlib import Path

import clr

clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")
import System.Drawing as D  # noqa: E402
import System.Windows.Forms as WF  # noqa: E402
from System import Action  # noqa: E402
from System.Threading import ApartmentState, Thread, ThreadStart  # noqa: E402

NO_BORDER = getattr(WF.FormBorderStyle, "None")     # .None в Python не написать — это ключевое слово
NO_EFFECT = getattr(WF.DragDropEffects, "None")

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MyPanel"
STORE = DATA_DIR / "shelf.json"
LOG = HERE / f"shelf-{os.environ.get("COMPUTERNAME", "pc")}.log"

SENSOR_H = 6        # высота невидимой полоски у верхнего края, px
DRAG_START = 10     # на сколько px сдвинуть мышь с зажатой кнопкой, чтобы включить датчик
DEBUG = False       # True — писать в shelf.log, что происходит при перетаскивании

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]

HWND_TOPMOST = wt.HWND(-1)
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
OFFSCREEN = -32000


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%d.%m %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


# ---------- список на полке (общий с панелью) ----------

def load_items():
    try:
        items = json.loads(STORE.read_text(encoding="utf-8"))
        return [p for p in items if isinstance(p, str)]
    except Exception:
        return []


def add_items(paths):
    """Перечитываем файл перед записью: панель могла что-то убрать с полки."""
    items = [p for p in load_items() if os.path.exists(p)]
    for p in map(str, paths):
        if p in items:
            items.remove(p)
        items.append(p)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, STORE)
    except Exception as e:
        log(f"save: {e!r}")
    return items


# ---------- окна ----------

def place(form, x, y, w, h):
    """Показать поверх всех, не забирая фокус у окна, в котором ты работаешь."""
    user32.SetWindowPos(wt.HWND(form.Handle.ToInt64()), HWND_TOPMOST, int(x), int(y), int(w), int(h),
                        SWP_NOACTIVATE | SWP_SHOWWINDOW)


def tool_window(form):
    """Без кнопки на панели задач и в Alt+Tab, скруглённые углы Windows 11."""
    hwnd = wt.HWND(form.Handle.ToInt64())
    ex = user32.GetWindowLongPtrW(hwnd, -20)
    user32.SetWindowLongPtrW(hwnd, -20, (ex | 0x80) & ~0x40000)   # WS_EX_TOOLWINDOW, без WS_EX_APPWINDOW
    v = ctypes.c_uint(2)
    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, wt.DWORD(33), ctypes.byref(v), wt.DWORD(4))


def fullscreen_app():
    """Игра, видео или презентация на весь экран — туда датчик не ставим, чтобы не выбить игру из полноэкранного режима.
    Размер окна не годится (панель задач бывает поверх окон), поэтому спрашиваем у самой Windows."""
    state = ctypes.c_int(0)
    try:
        if shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            return state.value in (2, 3, 4)   # полноэкранное приложение, D3D на весь экран, режим презентации
    except Exception:
        pass
    return False


SNIP_APPS = {"snippingtool", "screenclippinghost", "screensketch", "sharex", "lightshot", "greenshot", "flameshot"}


def foreground_process():
    hwnd = user32.GetForegroundWindow()
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).stem.lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def has_files(e):
    try:
        return bool(e.Data.GetDataPresent(WF.DataFormats.FileDrop))
    except Exception:
        return False


def is_honor():
    """Ноутбук Honor: у него своя системная полка (SuperHub), вторая не нужна. Смотрим производителя в BIOS."""
    if "HONOR" in os.environ.get("COMPUTERNAME", "").upper():
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as k:
            return "HONOR" in str(winreg.QueryValueEx(k, "SystemManufacturer")[0]).upper()
    except Exception:
        return False


# ---------- плашка «Положить на полку» ----------

BG = D.Color.FromArgb(243, 244, 247)
BG_HOT = D.Color.FromArgb(226, 236, 255)
TEXT = D.Color.FromArgb(28, 28, 32)
MUTED = D.Color.FromArgb(110, 112, 120)
ACCENT = D.Color.FromArgb(38, 99, 235)


class DropZone:
    def __init__(self, panel=None):
        dpi = user32.GetDpiForSystem() if hasattr(user32, "GetDpiForSystem") else 96
        self.k = dpi / 96
        self.panel = panel          # Api панели: тогда рисует она, а это окно — невидимый «ловец» поверх неё
        self.shown = False
        self.dropped = False
        self.hot = False
        self.hide_at = 0.0          # после броска плашка ещё секунду показывает, что положили

        s = self.s
        f = self.form = WF.Form()
        f.Text = "Полка"
        f.FormBorderStyle = NO_BORDER
        f.ShowInTaskbar = False
        f.TopMost = True
        f.StartPosition = WF.FormStartPosition.Manual
        f.BackColor = BG
        f.Size = D.Size(s(360), s(64))
        f.Location = D.Point(OFFSCREEN, OFFSCREEN)
        if panel:
            f.Opacity = 0.01                  # видно панель под ним; Windows всё равно отдаёт ему бросок
        f.AllowDrop = True
        f.DragEnter += self.on_drag_enter
        f.DragOver += self.on_drag_enter
        f.DragLeave += lambda *_: self.paint(False)
        f.DragDrop += self.on_drop

        self.title = self.label("Положить на полку", 11.5, TEXT, bold=True)
        self.sub = self.label("отпусти файл здесь", 9, MUTED)
        for lb in (self.title, self.sub):
            lb.AllowDrop = True              # бросить можно и на надпись, а не только мимо неё
            lb.DragEnter += self.on_drag_enter
            lb.DragOver += self.on_drag_enter
            lb.DragDrop += self.on_drop
            f.Controls.Add(lb)
        self.layout()

        self.sensors = []
        for scr in WF.Screen.AllScreens:
            sf = WF.Form()
            sf.FormBorderStyle = NO_BORDER
            sf.ShowInTaskbar = False
            sf.TopMost = True
            sf.StartPosition = WF.FormStartPosition.Manual
            sf.BackColor = D.Color.Black
            sf.Opacity = 0.01                 # невидима, но Windows считает её окном и сообщает, что над ней тащат
            sf.AllowDrop = True
            sf.Location = D.Point(OFFSCREEN, OFFSCREEN)
            sf.Size = D.Size(scr.Bounds.Width, SENSOR_H)
            sf.DragEnter += self.on_sensor
            self.sensors.append((sf, scr.Bounds))

    def s(self, v):
        return int(round(v * self.k))

    def label(self, text, size, color, bold=False):
        lb = WF.Label()
        lb.Text = text
        lb.AutoSize = True
        lb.Font = D.Font("Segoe UI Semibold" if bold else "Segoe UI", float(size))
        lb.ForeColor = color
        lb.BackColor = D.Color.Transparent
        return lb

    def layout(self):
        w = self.form.ClientSize.Width
        self.title.Location = D.Point((w - self.title.PreferredSize.Width) // 2, self.s(10))
        self.sub.Location = D.Point((w - self.sub.PreferredSize.Width) // 2, self.s(36))

    def say(self, title, sub, color=TEXT):
        self.title.Text, self.sub.Text = title, sub
        self.title.ForeColor = color
        self.layout()

    def paint(self, hot):
        if self.panel:
            if hot != self.hot:
                self.panel._drop_hot(hot)
        else:
            self.form.BackColor = BG_HOT if hot else BG
        self.hot = hot

    # --- перетаскивание ---
    def on_drag_enter(self, sender, e):
        ok = has_files(e)
        e.Effect = WF.DragDropEffects.Copy if ok else NO_EFFECT
        self.paint(ok)

    def on_drop(self, sender, e):
        self.paint(False)
        if not has_files(e):
            return
        paths = [str(p) for p in (e.Data.GetData(WF.DataFormats.FileDrop) or [])]
        if not paths:
            return
        self.dropped = True
        items = add_items(paths)
        if self.panel:
            self.hide()
            self.panel._drop_done(paths)
            return
        name = os.path.basename(paths[0].rstrip("\\/")) or paths[0]
        if len(name) > 30:
            name = name[:27] + "…"
        more = f" и ещё {len(paths) - 1}" if len(paths) > 1 else ""
        self.say("✓ Положил на полку", f"{name}{more} · Alt+Пробел → Полка", ACCENT)
        self.hide_at = time.time() + 1.6
        t = WF.Timer()
        t.Interval = 1700

        def tick(*_):
            t.Stop()
            t.Dispose()
            if time.time() >= self.hide_at:
                self.hide()
        t.Tick += tick
        t.Start()
        if DEBUG:
            log(f"положил {len(paths)}, всего {len(items)}")

    def on_sensor(self, sender, e):
        e.Effect = NO_EFFECT
        if DEBUG:
            try:
                log("над датчиком: " + ", ".join(str(f) for f in e.Data.GetFormats()))
            except Exception as ex:
                log(f"над датчиком: форматы не прочитать {ex!r}")
        if has_files(e) and not self.shown:
            # sender приходит из .NET новой обёрткой, поэтому «is» не работает — сравниваем окна по дескриптору
            src = sender.Handle.ToInt64() if sender is not None else None
            for sf, b in self.sensors:
                if sf.Handle.ToInt64() == src:
                    if self.panel:
                        self.show_over_panel(b)
                    else:
                        self.show(b)

    # --- показать / спрятать ---
    def show(self, b):
        self.say("Положить на полку", "отпусти файл здесь")
        self.paint(False)
        self.dropped = False
        w, h = self.form.Width, self.form.Height
        place(self.form, b.X + (b.Width - w) // 2, b.Y + SENSOR_H + self.s(6), w, h)
        self.shown = True

    def show_over_panel(self, b):
        """Панель превращается в приёмник; невидимый «ловец» ложится ровно поверх него."""
        try:
            rect = self.panel._drop_enter((b.X, b.Y, b.Width, b.Height))
        except Exception as e:
            log(f"панель: {e!r}")
            rect = None
        if not rect:
            return
        self.dropped = False
        self.hot = False
        place(self.form, *rect)
        self.shown = True

    def raise_over(self):
        """Панель, меняя форму, встаёт поверх всех — возвращаем «ловца» выше неё."""
        if self.shown:
            user32.SetWindowPos(wt.HWND(self.form.Handle.ToInt64()), HWND_TOPMOST, 0, 0, 0, 0,
                                0x0001 | 0x0002 | SWP_NOACTIVATE)      # NOSIZE | NOMOVE

    def hide(self):
        place(self.form, OFFSCREEN, OFFSCREEN, self.form.Width, self.form.Height)
        self.shown = False

    def sensors_on(self):
        for sf, b in self.sensors:
            place(sf, b.X, b.Y, b.Width, SENSOR_H)

    def sensors_off(self):
        for sf, b in self.sensors:
            place(sf, OFFSCREEN, OFFSCREEN, b.Width, SENSOR_H)

    def drag_finished(self):
        """Кнопку мыши отпустили. Если ничего не бросили — плашку убрать сразу, иначе она уйдёт сама."""
        self.sensors_off()
        if self.shown and not self.dropped:
            self.hide()
            if self.panel:
                threading.Thread(target=self.panel._drop_leave, daemon=True).start()

    def ui(self, fn):
        try:
            self.form.BeginInvoke(Action(fn))
        except Exception as e:
            log(f"ui: {e!r}")


# ---------- фоновый поток ----------

def watch_mouse(zone: DropZone):
    """Следит за левой кнопкой мыши: пока что-то тянут — у верхнего края лежит датчик."""
    pressed = armed = False
    start = (0, 0)
    pt = wt.POINT()
    tick = 0
    while True:
        time.sleep(0.025)
        tick += 1
        if armed and zone.shown and zone.panel and tick % 4 == 0:
            zone.ui(zone.raise_over)
        down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
        if down and not pressed:
            user32.GetCursorPos(ctypes.byref(pt))
            start = (pt.x, pt.y)
            armed = False
        elif down and not armed:
            user32.GetCursorPos(ctypes.byref(pt))
            if abs(pt.x - start[0]) + abs(pt.y - start[1]) >= DRAG_START:
                armed = True
                app = foreground_process()
                if app in SNIP_APPS:          # выделяют область для скриншота — поверх ничего не ставим
                    if DEBUG:
                        log(f"тянут в {app} — это скриншот, датчик не ставлю")
                elif fullscreen_app():
                    if DEBUG:
                        log("тянут, но открыто полноэкранное приложение — датчик не ставлю")
                else:
                    if DEBUG:
                        log(f"тянут мышью от {start} — ставлю датчик")
                    zone.ui(zone.sensors_on)
        elif not down and pressed:
            if armed:
                zone.ui(zone.drag_finished)
            armed = False
        pressed = down


def run_ui(panel=None):
    try:
        zone = DropZone(panel)
        prev = user32.GetForegroundWindow()
        for form in [zone.form] + [sf for sf, _ in zone.sensors]:
            form.Show()                # окна живут всё время, «спрятаны» — значит вынесены за экран
            tool_window(form)
        if prev:
            user32.SetForegroundWindow(prev)
        threading.Thread(target=watch_mouse, args=(zone,), daemon=True).start()
        WF.Application.Run()
    except Exception as e:
        log(f"run_ui: {e!r}")


def start_in_panel(panel):
    """Полка внутри процесса панели: свой STA-поток со своим циклом сообщений WinForms."""
    if is_honor():
        return
    kernel32.CreateMutexW(None, False, "Local\\MyPanel.Shelf")
    if kernel32.GetLastError() == 183:   # где-то ещё запущена отдельная полка
        log("полка уже запущена отдельно — в панели не запускаю")
        return
    t = Thread(ThreadStart(lambda: run_ui(panel)))
    t.SetApartmentState(ApartmentState.STA)
    t.IsBackground = True
    t.Start()


def main():
    if is_honor():
        return
    kernel32.CreateMutexW(None, False, "Local\\MyPanel.Shelf")
    if kernel32.GetLastError() == 183:   # уже запущена
        return
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    t = Thread(ThreadStart(run_ui))
    t.SetApartmentState(ApartmentState.STA)   # перетаскивание в Windows работает только в STA-потоке
    t.Start()
    t.Join()


if __name__ == "__main__":
    main()
