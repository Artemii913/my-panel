"""Моя панель — выезжает сверху по Alt+Пробел.

Вкладки: плеер (всё, что играет в Windows), переводчик, заготовки, быстрая заметка в Obsidian.
Запуск без консоли: pythonw panel.py (или «Запуск панели.vbs»).
"""
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")   # иначе numpy резервирует ~0,5–1,5 ГБ под потоки, которые нам не нужны

import asyncio
import base64
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import io
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import requests
import webview

# в собранной программе (.exe) файлы панели (ui.html) лежат во временной папке сборки — sys._MEIPASS
FROZEN = getattr(sys, "frozen", False)
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
VERSION = "1.0.0"
# Данные пользователя (настройки, ссылки, тексты) — отдельно от программы, чтобы обновление их не стирало:
# %APPDATA%\MyPanel. Рабочая копия, где они лежат рядом с программой (например, в OneDrive — общие
# для нескольких компьютеров), так и держит их там.
LOCAL_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MyPanel"
DATA_DIR = HERE if (HERE / "settings.json").exists() else Path(os.environ.get("APPDATA", Path.home())) / "MyPanel"
LOG_DIR = HERE if DATA_DIR == HERE else LOCAL_DIR / "logs"
SNIPPETS = DATA_DIR / "snippets.json"
# OneDrive у каждого компьютера свой путь (имя пользователя может отличаться) — берём из переменной Windows
ONEDRIVE = Path(os.environ.get("OneDrive") or Path.home() / "OneDrive")
DEFAULT_NOTES = Path.home() / "Documents" / "Моя панель — заметки"

TITLE = "Моя панель"
WIDTH, HEIGHT = 760, 560     # 760 — с семью вкладками в 660 было тесно
HOTKEY_MOD = 0x0001 | 0x4000  # Alt + без автоповтора
HOTKEY_VK = 0x20              # Пробел (Alt+Q занята другой программой)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


# ---------- буфер обмена ----------

CF_UNICODETEXT = 13
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowPos.argtypes =[wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.IsWindow.argtypes = [wt.HWND]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.MonitorFromPoint.restype = wt.HMONITOR
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.GetClipboardData.restype = wt.HANDLE
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]


def _open_clipboard():
    for _ in range(10):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def clipboard_get() -> str:
    if not _open_clipboard():
        return ""
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = kernel32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def clipboard_set(text: str) -> bool:
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(CF_UNICODETEXT, h)
        return True
    finally:
        user32.CloseClipboard()


def clipboard_set_image(path) -> bool:
    """Кладёт картинку в буфер как CF_DIB — её понимают чаты, браузеры, Telegram, Word."""
    from PIL import Image
    with Image.open(path) as im:
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "BMP")
    data = buf.getvalue()[14:]   # BMP без файлового заголовка = DIB
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0002, len(data))
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(8, h)   # CF_DIB
        return True
    finally:
        user32.CloseClipboard()


def send_ctrl_v():
    VK_CONTROL, VK_V, KEYUP = 0x11, 0x56, 0x0002
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    user32.keybd_event(VK_V, 0, KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYUP, 0)


# ---------- плеер: системные медиасессии Windows ----------

class Media:
    """Все вызовы WinRT идут через свой asyncio-цикл в отдельном потоке."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.mgr = None
        self.selected = None      # source_app_user_model_id выбранного источника
        self.covers = {}          # (source, title) -> data-uri

    def run(self, coro, timeout=4):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _mgr(self):
        if self.mgr is None:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as M,
            )
            self.mgr = await M.request_async()
        return self.mgr

    async def _sessions(self):
        mgr = await self._mgr()
        return list(mgr.get_sessions()), mgr.get_current_session()

    async def _pick(self):
        sessions, current = await self._sessions()
        for s in sessions:
            if s.source_app_user_model_id == self.selected:
                return s, sessions
        # без явного выбора — то, что сейчас играет: «текущий» источник Windows бывает не тем
        # (например, YouTube играет, а Windows считает текущей поставленную на паузу Яндекс Музыку)
        for s in sessions:
            try:
                if int(s.get_playback_info().playback_status) == 4:
                    return s, sessions
            except Exception:
                pass
        return (current or (sessions[0] if sessions else None)), sessions

    @staticmethod
    def app_name(app_id: str) -> str:
        a = app_id.lower()
        for key, name in (("yandex.desktop.music", "Яндекс Музыка"), ("yandex", "Яндекс Браузер"),
                          ("browser.exe", "Яндекс Браузер"), ("spotify", "Spotify"),
                          ("chrome", "Chrome"), ("msedge", "Edge"), ("firefox", "Firefox"),
                          ("telegram", "Telegram"), ("vlc", "VLC"), ("zune", "Медиаплеер"),
                          ("media", "Медиаплеер")):
            if key in a:
                return name
        return re.sub(r"(\.exe)?(!.*)?$", "", app_id.split("\\")[-1]) or "Плеер"

    async def _cover(self, s, props):
        key = (s.source_app_user_model_id, props.title)
        if key in self.covers:
            return self.covers[key]
        uri = None
        try:
            from winrt.windows.storage.streams import Buffer, InputStreamOptions
            ref = props.thumbnail
            if ref is not None:
                stream = await ref.open_read_async()
                size = int(stream.size)
                buf = Buffer(size)
                await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
                raw = bytes(memoryview(buf))[: buf.length]
                if raw:
                    uri = "data:image/png;base64," + base64.b64encode(raw).decode()
        except Exception:
            uri = None
        self.covers[key] = uri
        if len(self.covers) > 40:
            self.covers.pop(next(iter(self.covers)))
        return uri

    async def _state(self):
        s, sessions = await self._pick()
        sources = [{"id": x.source_app_user_model_id,
                    "name": self.app_name(x.source_app_user_model_id)} for x in sessions]
        if s is None:
            return {"ok": True, "empty": True, "sources": sources}
        props = await s.try_get_media_properties_async()
        pb = s.get_playback_info()
        tl = s.get_timeline_properties()
        playing = int(pb.playback_status) == 4
        pos = tl.position.total_seconds()
        end = tl.end_time.total_seconds()
        try:  # позиция в Windows обновляется редко — досчитываем сами
            if playing and tl.last_updated_time:
                pos += (dt.datetime.now(dt.timezone.utc) - tl.last_updated_time).total_seconds()
        except Exception:
            pass
        c = pb.controls
        return {
            "ok": True, "empty": False, "sources": sources,
            "source": s.source_app_user_model_id,
            "sourceName": self.app_name(s.source_app_user_model_id),
            "title": props.title or "", "artist": props.artist or "",
            "album": props.album_title or "",
            "playing": playing,
            "position": max(0.0, min(pos, end)) if end > 0 else 0.0,
            "duration": end,
            "canSeek": bool(c.is_playback_position_enabled) and end > 0,
            "canNext": bool(c.is_next_enabled), "canPrev": bool(c.is_previous_enabled),
            "cover": await self._cover(s, props),
        }

    async def _do(self, action, arg=None):
        s, _ = await self._pick()
        if s is None:
            return False
        if action == "toggle":
            return await s.try_toggle_play_pause_async()
        if action == "next":
            return await s.try_skip_next_async()
        if action == "prev":
            return await s.try_skip_previous_async()
        if action == "seek":
            tl = s.get_timeline_properties()
            cur = tl.position.total_seconds()
            try:
                if int(s.get_playback_info().playback_status) == 4:
                    cur += (dt.datetime.now(dt.timezone.utc) - tl.last_updated_time).total_seconds()
            except Exception:
                pass
            target = max(0.0, min(cur + float(arg), tl.end_time.total_seconds()))
            return await s.try_change_playback_position_async(int(target * 10_000_000))
        return False

    def state(self):
        try:
            return self.run(self._state())
        except Exception as e:
            return {"ok": False, "error": str(e), "sources": []}

    def do(self, action, arg=None):
        try:
            return bool(self.run(self._do(action, arg)))
        except Exception:
            return False


# ---------- громкость: вся система и отдельное приложение ----------

class Volume:
    """pycaw/COM. Каждый вызов инициализирует COM в своём потоке — pywebview зовёт API из разных потоков."""

    @staticmethod
    def _com():
        import comtypes
        try:
            comtypes.CoInitialize()
        except OSError:
            pass

    @staticmethod
    def _endpoint():
        from pycaw.pycaw import AudioUtilities
        return AudioUtilities.GetSpeakers().EndpointVolume

    @staticmethod
    def _app_sessions(source_id: str):
        """Аудиосессии процесса, который стоит за медиаисточником (chrome.exe, Яндекс Музыка.exe…)."""
        from pycaw.pycaw import AudioUtilities
        src = (source_id or "").lower()
        yandex = "yandex" in src or "яндекс" in src
        found = []
        for s in AudioUtilities.GetAllSessions():
            try:                    # сессия от уже закрытой программы падает на .Process — пропускаем её
                if not s.Process:
                    continue
                name = s.Process.name().lower()
            except Exception:
                continue
            stem = name.removesuffix(".exe")
            if (stem and stem in src) or (yandex and ("яндекс" in name or "yandex" in name)):
                found.append(s)
        return found

    def state(self, source_id=None):
        self._com()
        out = {}
        try:
            ep = self._endpoint()
            out["master"] = round(ep.GetMasterVolumeLevelScalar() * 100)
            out["muted"] = bool(ep.GetMute())
        except Exception:
            pass
        try:
            ss = self._app_sessions(source_id) if source_id else []
            if ss:
                out["app"] = round(ss[0].SimpleAudioVolume.GetMasterVolume() * 100)
        except Exception:
            pass
        return out

    def set_master(self, pct):
        self._com()
        ep = self._endpoint()
        ep.SetMasterVolumeLevelScalar(max(0.0, min(1.0, float(pct) / 100)), None)
        if float(pct) > 0 and ep.GetMute():
            ep.SetMute(0, None)
        return True

    def toggle_mute(self):
        self._com()
        ep = self._endpoint()
        ep.SetMute(0 if ep.GetMute() else 1, None)
        return bool(ep.GetMute())

    @staticmethod
    def nice_name(full: str) -> str:
        """«Динамики (7- USB Audio Device)» → «USB-наушники» и т.п. Непонятное оставляем как есть."""
        f = full.lower()
        if "usb" in f:
            return "USB-наушники"
        if "nvidia" in f or "hdmi" in f or "displayport" in f:
            return "Монитор " + full.split(" (")[0] if not full.lower().startswith("nvidia") else "Монитор"
        if "realtek" in f and "наушник" in f:
            return "Наушники (разъём)"
        if "realtek" in f:
            return "Динамики ноутбука"
        return re.sub(r"^(Динамики|Наушники|Speakers|Headphones) \((.+)\)$", r"\2", full)

    def outputs(self):
        """Активные устройства вывода звука и текущее по умолчанию."""
        self._com()
        from pycaw.pycaw import AudioUtilities
        from pycaw.constants import DEVICE_STATE, EDataFlow, ERole
        enum = AudioUtilities.GetDeviceEnumerator()
        default_id = enum.GetDefaultAudioEndpoint(EDataFlow.eRender.value, ERole.eMultimedia.value).GetId()
        coll = enum.EnumAudioEndpoints(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)
        out = []
        for i in range(coll.GetCount()):
            dev = AudioUtilities.CreateDevice(coll.Item(i))
            name = dev.FriendlyName or ""
            if "steam streaming" in name.lower():   # виртуальные устройства Steam — не нужны
                continue
            out.append({"id": dev.id, "name": self.nice_name(name), "full": name, "on": dev.id == default_id})
        return out

    def set_output(self, dev_id):
        self._com()
        from pycaw.pycaw import AudioUtilities
        from pycaw.constants import ERole
        AudioUtilities.SetDefaultDevice(dev_id, [ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])
        return True

    def set_app(self, source_id, pct):
        self._com()
        for s in self._app_sessions(source_id):
            s.SimpleAudioVolume.SetMasterVolume(max(0.0, min(1.0, float(pct) / 100)), None)
        return True


# ---------- переводчик ----------

def translate(text: str, target: str = "auto") -> dict:
    """target: "auto" (русский → en, остальное → ru), "ru" или "en"."""
    text = text.strip()
    if not text:
        return {"ok": False, "error": "Пусто"}
    if target not in ("ru", "en"):
        cyr = len(re.findall(r"[а-яё]", text, re.I))
        lat = len(re.findall(r"[a-z]", text, re.I))
        target = "en" if cyr > lat else "ru"
    try:  # основной: Google (адрес расширения Chrome — без лимита, как у gtx)
        r = requests.get("https://clients5.google.com/translate_a/t",
                         params={"client": "dict-chrome-ex", "sl": "auto", "tl": target, "q": text},
                         timeout=8)
        r.raise_for_status()
        data = r.json()
        first = data[0]
        out = first[0] if isinstance(first, list) else first
        src = first[1] if isinstance(first, list) and len(first) > 1 else ""
        if out:
            return {"ok": True, "text": out, "from": src, "to": target, "via": "Google"}
    except Exception:
        pass
    try:  # запасной: MyMemory
        pair = f"{'ru' if target == 'en' else 'en'}|{target}"
        r = requests.get("https://api.mymemory.translated.net/get",
                         params={"q": text[:500], "langpair": pair}, timeout=8)
        out = r.json()["responseData"]["translatedText"]
        return {"ok": True, "text": out, "from": pair[:2], "to": target, "via": "MyMemory"}
    except Exception as e:
        return {"ok": False, "error": f"Нет связи с переводчиком ({e.__class__.__name__})"}


# ---------- скриншоты ----------

def _known_screenshots_dir():
    """Папка скриншотов, которую Windows использует на этом компьютере (FOLDERID_Screenshots)."""
    try:
        import uuid
        guid = uuid.UUID("b7bede81-df94-4682-a7d8-57a52620b86f").bytes_le
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.c_char_p(guid), 0, None, ctypes.byref(out)) == 0:
            return Path(out.value)
    except Exception:
        pass
    return None


SHOT_DIRS = list(dict.fromkeys(  # без повторов, в порядке важности
    str(d).lower() for d in (_known_screenshots_dir(), ONEDRIVE / "Изображения" / "Screenshots",
                             ONEDRIVE / "Pictures" / "Screenshots", Path.home() / "Pictures" / "Screenshots") if d))
SHOT_DIRS = [Path(d) for d in SHOT_DIRS]
SHOT_EXT = {".png", ".jpg", ".jpeg"}
_thumbs = {}   # (path, mtime) -> data-uri


def recent_screenshots(n=9):
    files = []
    for d in SHOT_DIRS:
        if d.is_dir():
            try:
                with os.scandir(d) as it:
                    files += [(e.stat().st_mtime, e.path) for e in it if e.is_file() and Path(e.name).suffix.lower() in SHOT_EXT]
            except OSError:
                pass
    files.sort(reverse=True)
    out, now = [], dt.datetime.now()
    for mtime, path in files[:n]:
        key = (path, mtime)
        if key not in _thumbs:
            try:
                from PIL import Image
                with Image.open(path) as im:
                    im = im.convert("RGB")
                    im.thumbnail((360, 240))
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=82)
                _thumbs[key] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
            except Exception:
                _thumbs[key] = None
        t = dt.datetime.fromtimestamp(mtime)
        when = t.strftime("%H:%M") if t.date() == now.date() else (
            "вчера " + t.strftime("%H:%M") if (now.date() - t.date()).days == 1 else t.strftime("%d.%m %H:%M"))
        out.append({"path": path, "thumb": _thumbs[key], "when": when})
    if len(_thumbs) > 60:
        for k in list(_thumbs)[:-30]:
            _thumbs.pop(k, None)
    return out


# ---------- полка ----------

SHELF = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MyPanel" / "shelf.json"
_shelf_icons = {}


def shelf_paths():
    try:
        return [p for p in json.loads(SHELF.read_text(encoding="utf-8")) if isinstance(p, str)]
    except Exception:
        return []


def shelf_save(paths):
    SHELF.parent.mkdir(parents=True, exist_ok=True)
    tmp = SHELF.with_suffix(".tmp")
    tmp.write_text(json.dumps(paths, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, SHELF)


class _SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", wt.HICON), ("iIcon", ctypes.c_int), ("dwAttributes", wt.DWORD),
                ("szDisplayName", wt.WCHAR * 260), ("szTypeName", wt.WCHAR * 80)]


def _file_icon(path):
    """Значок как в Проводнике (Word, папка, PDF…) в виде data-uri; для картинок — миниатюра."""
    if Path(path).suffix.lower() in SHOT_EXT:
        try:
            from PIL import Image
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((160, 120))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=80)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(), True
        except Exception:
            pass
    try:
        ctypes.windll.ole32.CoInitialize(None)
        info = _SHFILEINFOW()
        shell32 = ctypes.windll.shell32
        shell32.SHGetFileInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(_SHFILEINFOW), wt.UINT, wt.UINT]
        if shell32.SHGetFileInfoW(path, 0, ctypes.byref(info), ctypes.sizeof(info), 0x100) and info.hIcon:  # SHGFI_ICON
            import clr
            clr.AddReference("System.Drawing")
            import System
            import System.Drawing as Drawing
            try:
                bmp = Drawing.Icon.FromHandle(System.IntPtr(info.hIcon)).ToBitmap()
                ms = System.IO.MemoryStream()
                bmp.Save(ms, Drawing.Imaging.ImageFormat.Png)
                return "data:image/png;base64," + base64.b64encode(bytes(ms.ToArray())).decode(), False
            finally:
                ctypes.windll.user32.DestroyIcon(wt.HICON(info.hIcon))
    except Exception:
        pass
    return None, False


def _size_text(path):
    if os.path.isdir(path):
        return "папка"
    try:
        n = os.path.getsize(path)
    except OSError:
        return ""
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024


def shelf_list():
    """Что лежит на полке, новые первыми. Пропавшие файлы (удалили, переместили) убираем."""
    paths = shelf_paths()
    alive = [p for p in paths if os.path.exists(p)]
    if alive != paths:
        shelf_save(alive)
    out = []
    for p in reversed(alive):
        try:
            key = (p, os.path.getmtime(p))
        except OSError:
            continue
        if key not in _shelf_icons:
            _shelf_icons[key] = _file_icon(p)
        icon, thumb = _shelf_icons[key]
        out.append({"path": p, "name": os.path.basename(p.rstrip("\\/")) or p, "meta": _size_text(p),
                    "icon": icon, "thumb": thumb})
    if len(_shelf_icons) > 80:
        for k in list(_shelf_icons)[:-40]:
            _shelf_icons.pop(k, None)
    return out


# ---------- таймер (общий с Джарвисом: файл %LOCALAPPDATA%\\MyPanel\\timer.json) ----------

TIMER = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MyPanel" / "timer.json"


def timer_read():
    try:
        return json.loads(TIMER.read_text(encoding="utf-8"))
    except Exception:
        return {}


def timer_write(d):
    TIMER.parent.mkdir(parents=True, exist_ok=True)
    tmp = TIMER.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, TIMER)


def timer_state():
    d = timer_read()
    end = d.get("end")
    if not end or d.get("fired"):
        return {"active": False, "done_at": d.get("fired_at")}
    return {"active": True, "remaining": max(0.0, end - time.time()), "total": d.get("total", 0)}


def timer_alarm(total):
    """Время вышло: три коротких сигнала и уведомление в углу экрана."""
    import subprocess
    import winsound
    mins = round(total / 60)
    what = f"{mins} мин" if mins >= 1 else f"{int(total)} с"
    ps = ("Add-Type -AssemblyName System.Windows.Forms; $n=New-Object System.Windows.Forms.NotifyIcon; "
          "$n.Icon=[System.Drawing.SystemIcons]::Information; $n.Visible=$true; "
          f"$n.ShowBalloonTip(6000,'Таймер','Время вышло ({what})',[System.Windows.Forms.ToolTipIcon]::None); "
          "Start-Sleep 7; $n.Dispose()")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=0x08000000)
    except Exception:
        pass
    for _ in range(3):
        for f in (880, 1175):
            winsound.Beep(f, 180)
        time.sleep(0.35)


def timer_loop():
    """Следит за таймером, кто бы его ни поставил — панель или Джарвис."""
    while True:
        time.sleep(0.5)
        d = timer_read()
        if d.get("end") and not d.get("fired") and time.time() >= d["end"]:
            d["fired"], d["fired_at"] = True, time.time()
            timer_write(d)
            try:
                timer_alarm(d.get("total", 0))
            except Exception:
                pass


# ---------- быстрые ссылки ----------

LINKS = DATA_DIR / "links.json"


SETTINGS = DATA_DIR / "settings.json"
ALL_TABS = ["player", "tr", "snip", "links", "shots", "shelf", "note"]
DEFAULT_SETTINGS = {
    # сайты открываем в Chrome и всегда в одном профиле — иначе Windows отдаёт их Edge (браузер по умолчанию),
    # а Chrome без указания профиля берёт последний открытый, и вход в GitHub и др. то есть, то нет
    "browser": "chrome",            # chrome | edge | default (браузер Windows по умолчанию)
    "chrome_profile": "Default",
    "theme": "system",              # system (как в Windows) | light | dark
    "tabs": ALL_TABS,               # какие вкладки показывать
    "tab_order": ALL_TABS,          # порядок вкладок (перетаскивание долгим нажатием)
    "notes_dir": "",                # куда пишет «Заметка»; пусто — Документы\Моя панель — заметки
    "notes_obsidian": False,        # папка — из Obsidian: в начало файла дня пишем свойства (дата, тип)
    "music_app": "yandex",          # что открыть кнопкой в пустом плеере: yandex | spotify | ytmusic | none
    "shelf": True,                  # файл, принесённый к верхнему краю, кладётся на полку
    "calendar_ics": "",             # секретные iCal-ссылки календарей (Google Календарь → Интеграция), через пробел
    "onboarded": False,             # приветствие и экскурсия уже показаны
}


def settings():
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS.read_text(encoding="utf-8"))}
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(patch):
    """Сохраняет изменённые ключи; чужие (например, настройки Джарвиса) остаются как были."""
    try:
        cur = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    cur.update({k: v for k, v in (patch or {}).items() if k in DEFAULT_SETTINGS})
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(SETTINGS) + ".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS)
    return {**DEFAULT_SETTINGS, **cur}


SAMPLE_SNIPPETS = [
    {"group": "Пример", "label": "Почта", "text": "my@mail.ru"},
    {"group": "Пример", "label": "Спасибо", "text": "Спасибо, получил!"},
]
SAMPLE_LINKS = [
    {"group": "Пример", "label": "YouTube", "open": "https://youtube.com"},
    {"group": "Пример", "label": "Загрузки", "open": "%USERPROFILE%\\Downloads"},
]


def ensure_data():
    """Первый запуск у нового человека: папка данных и примеры, чтобы вкладки не были пустыми."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for path, sample in ((SNIPPETS, SAMPLE_SNIPPETS), (LINKS, SAMPLE_LINKS)):
        if not path.exists():
            path.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")


def system_dark():
    """Тёмная тема приложений в Windows (Параметры → Персонализация → Цвета)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


# ---------- автозапуск: ярлык в папке «Автозагрузка» ----------

def startup_link():
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Моя панель.lnk"


def set_autostart(on):
    link = startup_link()
    if not on:
        link.unlink(missing_ok=True)
        return False
    import subprocess
    import sys
    if getattr(sys, "frozen", False):               # собранная программа (.exe)
        target, args = sys.executable, ""
    elif (HERE / "Запустить панель.vbs").exists():
        target, args = str(HERE / "Запустить панель.vbs"), ""
    else:
        target, args = str(Path(sys.executable).with_name("pythonw.exe")), f'"{HERE / "panel.py"}"'
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
          "$s.TargetPath=$env:TGT;$s.Arguments=$env:ARGS;$s.WorkingDirectory=$env:WD;"
          "$s.Description='Моя панель — Alt+Пробел';$s.Save()")
    env = {**os.environ, "LNK": str(link), "TGT": target, "ARGS": args, "WD": str(HERE)}
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, creationflags=0x08000000, timeout=20)
    return link.exists()


# ---------- календарь: ближайшие события из iCal-ссылок (Google Календарь и др.) ----------

CAL = {"at": 0.0, "events": [], "error": None, "src": None, "busy": False}


def calendar_urls():
    raw = settings().get("calendar_ics") or ""
    return [("https://" + u[9:]) if u.startswith("webcal://") else u for u in re.split(r"[\s,]+", raw) if u.strip()]


def calendar_refresh(force=False):
    """Скачивает календари раз в 10 минут; события на ближайшие двое суток, повторяющиеся тоже."""
    urls = calendar_urls()
    if not urls:
        CAL.update(events=[], error=None, src=None)
        return
    if CAL["busy"] or (not force and time.time() - CAL["at"] < 600 and CAL["src"] == urls):
        return
    CAL["busy"] = True
    try:
        import icalendar
        import recurring_ical_events
        now = dt.datetime.now().astimezone()
        events, error = [], None
        for url in urls:
            try:
                r = requests.get(url, timeout=15)
                r.raise_for_status()
                cal = icalendar.Calendar.from_ical(r.content)
                for e in recurring_ical_events.of(cal).between(now - dt.timedelta(hours=12), now + dt.timedelta(days=2)):
                    start = e.get("DTSTART").dt
                    end = e.get("DTEND").dt if e.get("DTEND") else None
                    allday = not isinstance(start, dt.datetime)
                    if allday:
                        start = dt.datetime.combine(start, dt.time()).astimezone()
                        end = dt.datetime.combine(end, dt.time()).astimezone() if end else start + dt.timedelta(days=1)
                    else:
                        start = start.astimezone() if start.tzinfo else start.replace(tzinfo=now.tzinfo)
                        end = (end.astimezone() if end.tzinfo else end.replace(tzinfo=now.tzinfo)) if end else start + dt.timedelta(hours=1)
                    events.append({"title": str(e.get("SUMMARY") or "Событие"), "loc": str(e.get("LOCATION") or ""),
                                   "start": start.timestamp(), "end": end.timestamp(), "allday": allday})
            except Exception as ex:
                error = f"{type(ex).__name__}: {ex}"[:160]
        events.sort(key=lambda x: x["start"])
        CAL.update(at=time.time(), events=events, error=error, src=urls)
    finally:
        CAL["busy"] = False


def calendar_state():
    """Для шторки и настроек: ближайшие (ещё не закончившиеся) события. Скачивание — в фоне."""
    threading.Thread(target=calendar_refresh, daemon=True).start()
    now = time.time()
    upcoming = [e for e in CAL["events"] if e["end"] > now and not e["allday"]]
    return {"on": bool(calendar_urls()), "events": upcoming[:5], "error": CAL["error"],
            "today": sum(1 for e in CAL["events"] if not e["allday"] and dt.date.fromtimestamp(e["start"]) == dt.date.today()),
            "loaded": CAL["at"] > 0}


# ---------- обновления: новые версии — в Releases на GitHub ----------

UPDATE_REPO = "Artemii913/my-panel"
UPD = {"at": 0.0, "latest": None, "url": None, "notes": "", "error": None}


def version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).lstrip("vV").split("."))
    except Exception:
        return (0,)


def check_update():
    """Последний релиз на GitHub: версия, установщик (.exe) и что нового."""
    try:
        r = requests.get(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                         headers={"Accept": "application/vnd.github+json"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        exe = next((a["browser_download_url"] for a in data.get("assets", []) if a["name"].lower().endswith(".exe")), None)
        UPD.update(at=time.time(), latest=data.get("tag_name", "").lstrip("vV"), url=exe,
                   notes=(data.get("body") or "")[:800], error=None)
    except Exception as e:
        UPD.update(at=time.time(), error=f"{type(e).__name__}")
    return update_info()


def update_info():
    return {"current": VERSION, "latest": UPD["latest"], "notes": UPD["notes"], "error": UPD["error"],
            "available": bool(UPD["latest"] and UPD["url"] and version_tuple(UPD["latest"]) > version_tuple(VERSION)),
            "installed": FROZEN}


def install_update():
    """Скачать установщик новой версии и запустить его тихо: он закроет панель, обновит и запустит снова."""
    if not (FROZEN and UPD["url"]):
        return False
    import subprocess
    import tempfile
    path = Path(tempfile.gettempdir()) / f"MyPanel-Setup-{UPD['latest']}.exe"
    with requests.get(UPD["url"], stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"])
    return True


def update_loop(api):
    """Раз в сутки (и через минуту после запуска) — проверить, нет ли новой версии; если есть — сказать один раз."""
    time.sleep(60)
    told = None
    while True:
        info = check_update()
        if info["available"] and info["installed"] and told != info["latest"]:
            told = info["latest"]
            api._js(f"window.updateAvailable && updateAvailable({json.dumps(info['latest'])})")
        time.sleep(24 * 3600)


# ---------- музыка: что открыть, когда ничего не играет ----------

MUSIC_APPS = {   # (ссылка на приложение, сайт — если приложения нет)
    "yandex": ("yandexmusic://", "https://music.yandex.ru"),
    "spotify": ("spotify:", "https://open.spotify.com"),
    "ytmusic": (None, "https://music.youtube.com"),
}


def open_music():
    proto, web = MUSIC_APPS.get(settings().get("music_app"), (None, None))
    if proto:
        try:
            os.startfile(proto)
            return True
        except OSError:
            pass
    return open_target(web) if web else False


def chrome_exe():
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if p.exists():
                return p
    return None


def chrome_profiles():
    """Профили Chrome: [{"dir": "Default", "name": "Иван"}, …] — чтобы выбрать нужный в настройках."""
    try:
        state = Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data" / "Local State"
        cache = json.loads(state.read_text(encoding="utf-8"))["profile"]["info_cache"]
        return [{"dir": k, "name": v.get("name", k)} for k, v in cache.items()]
    except Exception:
        return []


def open_target(target: str) -> bool:
    """Сайт — в Chrome в выбранном профиле; папка, файл, obsidian:// — тем, чем положено в Windows."""
    target = os.path.expandvars(target.strip())
    if not target:
        return False
    if target.lower().startswith(("http://", "https://")):
        s = settings()
        exe = chrome_exe() if s.get("browser") == "chrome" else None
        if exe:
            import subprocess
            subprocess.Popen([str(exe), f"--profile-directory={s['chrome_profile']}", target])
            return True
        if s.get("browser") == "edge":
            os.startfile("microsoft-edge:" + target)
            return True
    os.startfile(target)
    return True


def open_in_editor(path):
    """Открыть файл программой по умолчанию, а если для .json её нет — в Блокноте."""
    try:
        os.startfile(path)
    except OSError:
        import subprocess
        subprocess.Popen(["notepad.exe", str(path)])
    return True


def save_list(path, items, value_key):
    """Сохраняет заготовки/ссылки, отредактированные в самой панели. Пустые строки выкидываем."""
    clean = []
    for it in items or []:
        label = str(it.get("label", "")).strip()
        value = str(it.get(value_key, "")).strip()
        if not (label and value):
            continue
        row = {"group": str(it.get("group", "")).strip() or "Разное", "label": label, value_key: value}
        if it.get("icon"):
            row["icon"] = it["icon"]
        clean.append(row)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)   # атомарно: OneDrive не увидит полузаписанный файл
    return True


# ---------- быстрые заметки в Obsidian ----------

def notes_dir() -> Path:
    d = settings().get("notes_dir")
    return Path(os.path.expandvars(d)) if d else DEFAULT_NOTES


def note_path(day: dt.date) -> Path:
    return notes_dir() / f"{day.isoformat()} быстрые заметки.md"


def save_note(text: str) -> dict:
    text = text.strip()
    if not text:
        return {"ok": False, "error": "Пусто"}
    now = dt.datetime.now()
    path = note_path(now.date())
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        props = (f"---\nдата: {now.date().isoformat()}\nтип: заметка\nпроект: Входящие\n---\n\n"
                 if settings().get("notes_obsidian") else "")
        path.write_text(props + f"# Быстрые заметки {now.strftime('%d.%m.%Y')}\n\n", encoding="utf-8")
    lines = text.splitlines()
    entry = f"- **{now.strftime('%H:%M')}** {lines[0]}\n" + "".join(f"  {l}\n" for l in lines[1:])
    with path.open("a", encoding="utf-8") as f:
        f.write(entry)
    return {"ok": True, "notes": today_notes()}


def today_notes():
    path = note_path(dt.date.today())
    if not path.exists():
        return []
    items = re.findall(r"^- \*\*(\d\d:\d\d)\*\* (.*)$", path.read_text(encoding="utf-8"), re.M)
    return [{"time": t, "text": x} for t, x in items][-5:][::-1]


# ---------- окно и горячая клавиша ----------

class Api:
    def __init__(self):
        self._media = Media()
        self._volume = Volume()
        self._window = None
        self._prev = None
        self._state = "hidden"
        self._pinned = False
        self._had_focus = False
        self._mon = None
        self._size = (WIDTH, HEIGHT)
        self._glass = None
        self._lock = threading.RLock()
        self._dropping = False          # тащат файл к верхнему краю — панель стала приёмником полки
        self._drop = {}
        self._strip = [STRIP_W, STRIP_H, STRIP_R]   # к какой форме шторка стремится (физические пиксели)
        self._strip_cur = [float(STRIP_W), float(STRIP_H), float(STRIP_R), 0.0]   # что видно сейчас: w, h, r, сдвиг x
        self._strip_v = [0.0, 0.0, 0.0, 0.0]        # скорости пружины
        self._strip_x0 = None                       # где окно шторки стоит без сдвига к курсору

    # плеер
    def media_state(self):
        return self._media.state()

    def media_do(self, action, arg=None):
        return self._media.do(action, arg)

    def media_select(self, source_id):
        self._media.selected = source_id
        return True

    def volume_state(self, source_id=None):
        try:
            return self._volume.state(source_id)
        except Exception:
            return {}

    def volume_set(self, kind, pct, source_id=None):
        try:
            if kind == "master":
                return self._volume.set_master(pct)
            return self._volume.set_app(source_id, pct)
        except Exception:
            return False

    def outputs(self):
        try:
            return self._volume.outputs()
        except Exception as e:
            self.log(f"outputs: {e!r}")
            return []

    def set_output(self, dev_id):
        try:
            return self._volume.set_output(dev_id)
        except Exception as e:
            self.log(f"set_output: {e!r}")
            return False

    def volume_mute(self):
        try:
            return self._volume.toggle_mute()
        except Exception:
            return None

    # переводчик
    def translate(self, text, target="auto"):
        return translate(text, target)

    def clipboard(self):
        return clipboard_get()

    def clipboard_seq(self):
        """Меняется при каждом копировании — по нему страница понимает, что в буфере новое."""
        return int(user32.GetClipboardSequenceNumber())

    def copy(self, text):
        return clipboard_set(text)

    # заготовки
    def snippets(self):
        try:
            return json.loads(SNIPPETS.read_text(encoding="utf-8"))
        except Exception as e:
            return [{"group": "Ошибка", "label": "snippets.json не читается", "text": str(e)}]

    def paste_snippet(self, text):
        """Копирует и вставляет в последнее окно, где ты был до панели. Панель остаётся открытой."""
        clipboard_set(text)
        target = self._prev
        if target and user32.IsWindow(target):
            time.sleep(0.05)
            user32.SetForegroundWindow(target)
            time.sleep(0.1)
            send_ctrl_v()
            return True
        return False

    def open_snippets_file(self):
        return open_in_editor(SNIPPETS)

    def save_snippets(self, items):
        return save_list(SNIPPETS, items, "text")

    def save_links(self, items):
        return save_list(LINKS, items, "open")

    # скриншоты
    def screenshots(self):
        try:
            return recent_screenshots()
        except Exception as e:
            self.log(f"screenshots: {e!r}")
            return []

    def shot_copy(self, path):
        try:
            return clipboard_set_image(path)
        except Exception as e:
            self.log(f"shot_copy: {e!r}")
            return False

    def shot_paste(self, path):
        """Картинку в буфер и сразу Ctrl+V в последнее окно, где ты был (чат, Telegram…)."""
        if not self.shot_copy(path):
            return False
        target = self._prev
        if target and user32.IsWindow(target):
            time.sleep(0.05)
            user32.SetForegroundWindow(target)
            time.sleep(0.12)
            send_ctrl_v()
            return True
        return False

    def drag_shot(self, path):
        """Настоящее перетаскивание Windows (как из Проводника): отдаём сам файл и картинку.
        Зовётся со страницы, пока кнопка мыши ещё зажата; DoDragDrop должен идти в потоке окна."""
        form = getattr(self._window, "native", None)
        if form is None or not os.path.isfile(path):
            return False
        # только пока левая кнопка реально зажата: иначе Windows сразу «бросит» файл в окно под курсором
        if not (user32.GetAsyncKeyState(0x01) & 0x8000):
            return False
        from System import Array, Func, String, Type
        import System.Drawing as Drawing
        import System.Windows.Forms as WinForms
        result = {}

        def run():
            data = WinForms.DataObject()
            data.SetData(WinForms.DataFormats.FileDrop, Array[String]([path]))
            img = None
            try:
                img = Drawing.Image.FromFile(path)
                data.SetData(WinForms.DataFormats.Bitmap, img)
            except Exception:
                pass
            try:
                result["effect"] = str(form.DoDragDrop(data, WinForms.DragDropEffects.Copy))
            finally:
                if img is not None:
                    img.Dispose()
            return None

        try:
            form.Invoke(Func[Type](run))
        except Exception as e:
            self.log(f"drag_shot: {e!r}")
            return False
        return result.get("effect", "None") != "None"

    def shot_open(self, path):
        os.startfile(path)
        return True

    # таймер
    def timer_state(self):
        return timer_state()

    def timer_start(self, seconds):
        seconds = max(1, int(float(seconds)))
        timer_write({"end": time.time() + seconds, "total": seconds, "by": "панель"})
        return timer_state()

    def timer_add(self, seconds):
        d = timer_read()
        if d.get("end") and not d.get("fired"):
            d["end"] += seconds
            d["total"] = d.get("total", 0) + seconds
            timer_write(d)
        return timer_state()

    def timer_cancel(self):
        timer_write({})
        return timer_state()

    # полка: кладут файлы через плашку у верхнего края (shelf.py), здесь их достают
    def shelf_items(self):
        try:
            return shelf_list()
        except Exception as e:
            self.log(f"shelf_items: {e!r}")
            return []

    def shelf_remove(self, path):
        shelf_save([p for p in shelf_paths() if p != path])
        return True

    def shelf_clear(self):
        shelf_save([])
        return True

    def shelf_reveal(self, path):
        import subprocess
        subprocess.Popen(["explorer", "/select,", path], creationflags=0x08000000)
        return True

    def drag_files(self, paths):
        """Как drag_shot, но для любых файлов и папок с полки, можно сразу несколько. Только копия."""
        form = getattr(self._window, "native", None)
        paths = [p for p in (paths or []) if os.path.exists(p)]
        if form is None or not paths or not (user32.GetAsyncKeyState(0x01) & 0x8000):
            return False
        from System import Array, Func, String, Type
        import System.Windows.Forms as WinForms
        result = {}

        def run():
            data = WinForms.DataObject()
            data.SetData(WinForms.DataFormats.FileDrop, Array[String](paths))
            result["effect"] = str(form.DoDragDrop(data, WinForms.DragDropEffects.Copy))
            return None

        try:
            form.Invoke(Func[Type](run))
        except Exception as e:
            self.log(f"drag_files: {e!r}")
            return False
        return result.get("effect", "None") != "None"

    def open_shots_folder(self):
        for d in SHOT_DIRS:
            if d.is_dir():
                os.startfile(d)
                return True
        return False

    # быстрые ссылки
    def links(self):
        try:
            return json.loads(LINKS.read_text(encoding="utf-8"))
        except Exception as e:
            return [{"group": "Ошибка", "label": "links.json не читается", "open": str(e)}]

    def open_link(self, target):
        try:
            return open_target(target)
        except Exception as e:
            self.log(f"open_link: {e!r}")
            return False

    def open_links_file(self):
        return open_in_editor(LINKS)

    def browser_info(self):
        return {"profiles": chrome_profiles(), "current": settings().get("chrome_profile"), "chrome": bool(chrome_exe())}

    def set_chrome_profile(self, profile_dir):
        save_settings({"chrome_profile": profile_dir})
        return True

    # заметки
    def save_note(self, text):
        return save_note(text)

    def today_notes(self):
        return today_notes()

    def open_inbox(self):
        d = notes_dir()
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(d)

    # настройки
    def get_config(self):
        return {"settings": settings(), "version": VERSION, "data_dir": str(DATA_DIR),
                "notes_dir": str(notes_dir()), "system_dark": system_dark(),
                "autostart": startup_link().exists(), "chrome": bool(chrome_exe()),
                "profiles": chrome_profiles()}

    def set_config(self, patch):
        return save_settings(patch)

    def pick_folder(self):
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or getattr(webview, "FOLDER_DIALOG", 20)
        res = self._window.create_file_dialog(kind)
        return res[0] if res else None

    def open_data_dir(self):
        os.startfile(DATA_DIR)

    def set_autostart(self, on):
        return set_autostart(bool(on))

    def open_music(self):
        return open_music()

    def update_info(self):
        return update_info()

    def update_check(self):
        return check_update()

    def update_install(self):
        return install_update()

    def log(self, msg):
        with (LOG_DIR / f"panel-{os.environ.get("COMPUTERNAME", "pc")}.log").open("a", encoding="utf-8") as f:   # свой файл на каждом компьютере
            f.write(f"{dt.datetime.now():%H:%M:%S} {msg}\n")

    # ---------- окно: скрыта / открыта / шторка / подглядывание ----------
    # hidden — нет на экране; open — развёрнута; strip — язычок сверху экрана;
    # peek — развернулась от наведения на язычок, пока ты в неё не кликнул.

    def _hwnd(self):
        return user32.FindWindowW(None, TITLE)

    def close(self):
        return self.hide()

    def is_visible(self):
        return self._state != "hidden"

    def mode(self):
        return self._state

    def set_pinned(self, on):
        self._pinned = bool(on)
        return self._pinned

    def glass(self):
        """Размытый снимок экрана под панелью — фон «матового стекла»."""
        return self._glass

    def hide(self):
        with self._lock:
            if self._state == "hidden":
                return True
            hwnd = self._hwnd()
            if hwnd:
                r = rect_of(hwnd)
                if not self._shrink(hwnd, r):
                    slide(hwnd, r.left, r.top, self._mon.top - (r.bottom - r.top) - 20, SLIDE_OUT, ease_in)
            self._state = "hidden"
            self._window.hide()
        return True

    # «Вырез», как у Coucou/Dynamic Island: панель приклеена к верхнему краю экрана и растёт из шторки.
    # Окно сразу полного размера, а видимую часть задаёт регион окна (SetWindowRgn) — его и анимируем.
    # За пределами региона окна просто нет: ни рамки, ни тени Windows, клики проходят насквозь.

    def _js(self, code):
        threading.Thread(target=self._window.evaluate_js, args=(code,), daemon=True).start()

    # ---------- полка: файл тащат к верхнему краю — принимает сама панель (зовёт shelf.py) ----------
    # Шторка (или скрытая панель) расширяется в «Положить на полку», открытая панель сжимается в полоску.
    # Сам бросок ловит невидимое окно shelf.py, которое лежит ровно поверх этой области.

    def _drop_enter(self, bounds):
        """bounds — (x, y, w, h) монитора, у края которого тащат файл.
        Возвращает (x, y, w, h) на экране — куда бросать; None — панель ещё не готова."""
        with self._lock:
            if self._dropping:
                return self._drop["rect"]
            hwnd = self._hwnd()
            if not hwnd or not settings().get("shelf", True):
                return None
            bx, by, bw, bh = bounds
            on_mon = self._mon is not None and bx <= (self._mon.left + self._mon.right) // 2 < bx + bw \
                and by <= self._mon.top < by + bh
            self._dropping = True
            d = self._drop = {"prev": self._state}
            if self._state in ("open", "peek") and on_mon:
                r = rect_of(hwnd)
                w, h = r.right - r.left, r.bottom - r.top
                d.update(mode="band", win=(w, h), rect=(r.left, r.top, w, self._px(DROP_BAND_H)))
            else:
                if self._state in ("open", "peek"):          # панель открыта на другом мониторе — прячем её
                    clear_shape(hwnd)
                    self._window.hide()
                    d["prev"] = "hidden"
                self._mon = wt.RECT(bx, by, bx + bw, by + bh)
                w, h = self._panel_size()
                dw, dh = self._px(DROP_W), self._px(DROP_H)
                d.update(mode="strip", win=(w, h), pos=(bx + (bw - w) // 2, by),
                         rect=(bx + (bw - dw) // 2, by, dw, dh))
            threading.Thread(target=self._drop_in, daemon=True).start()
            return d["rect"]

    def _drop_in(self):
        with self._lock:
            hwnd, d = self._hwnd(), self._drop
            w, h = d["win"]
            if d["mode"] == "band":
                self._js(f"dropMode(true, {w}, {self._px(DROP_BAND_H)})")
                animate_shape(hwnd, w, (w, h, self._px(PANEL_R)), (w, self._px(DROP_BAND_H), self._px(PANEL_R)), DROP_ANIM, grow=False)
                return
            self._js(f"dropMode(true, {self._px(DROP_W)}, {self._px(DROP_H)})")
            x, y = d["pos"]
            set_shape(hwnd, w, *[int(v) for v in self._strip_cur[:3]], redraw=False)
            place(hwnd, x, y, w, h)
            if d["prev"] == "hidden":
                self._window.show()
                time.sleep(0.05)
                place(hwnd, x, y, w, h)
            animate_shape(hwnd, w, tuple(int(v) for v in self._strip_cur[:3]), self._px3(DROP_W, DROP_H, DROP_R), DROP_ANIM, grow=True)

    def _drop_hot(self, on):
        if self._dropping:
            self._js(f"dropHot({'true' if on else 'false'})")

    def _drop_done(self, paths):
        """Бросили: показать «✓ Положил на полку» и через секунду вернуться как было."""
        name = os.path.basename(paths[0].rstrip("\\/")) or paths[0]
        if len(name) > 34:
            name = name[:31] + "…"
        more = f" и ещё {len(paths) - 1}" if len(paths) > 1 else ""
        self._js(f"dropDone({json.dumps('Положил на полку')}, {json.dumps(name + more)})")
        self._drop["caught"] = name + more
        threading.Timer(1.1, self._drop_leave, args=(True,)).start()

    def _drop_leave(self, dropped=False):
        """Файл бросили или отпустили мимо — вернуться в прежнее состояние."""
        with self._lock:
            if not self._dropping:
                return
            hwnd, d = self._hwnd(), self._drop
            w, h = d["win"]
            if d["mode"] == "band":
                self._js("dropMode(false)" + ("; show('shelf')" if dropped else ""))
                animate_shape(hwnd, w, (w, self._px(DROP_BAND_H), self._px(PANEL_R)), (w, h, self._px(PANEL_R)), DROP_ANIM, grow=True)
            else:
                animate_shape(hwnd, w, self._px3(DROP_W, DROP_H, DROP_R), tuple(self._strip), DROP_ANIM, grow=False)
                self._js("dropMode(false)")
                if d["prev"] == "hidden":
                    self._window.hide()
                    self._state = "hidden"
                else:
                    self._strip_place(hwnd)
                    self._state = "strip"
                    if d.get("caught"):
                        self._js(f"window.islFlash && islFlash({json.dumps(d['caught'])})")
            self._dropping = False

    def _shrink(self, hwnd, r):
        """Сжаться в шторку. False — режим выреза выключен, пусть работает старая анимация."""
        if not NOTCH:
            return False
        w, h = r.right - r.left, r.bottom - r.top
        self._js("window.notchOut && notchOut()")      # содержимое гаснет, пока форма сжимается
        animate_shape(hwnd, w, (w, h, self._px(PANEL_R)), tuple(self._strip), NOTCH_OUT, grow=False)
        return True

    def toggle(self):
        hwnd = self._hwnd()
        if not hwnd or self._dropping:
            return
        if self._state == "open":
            if user32.GetForegroundWindow() != hwnd:
                user32.SetForegroundWindow(hwnd)   # открыта, но ты в другом окне — вернуть наверх
                focus_page(self._window) or focus_webview(hwnd)
                return
            self.hide()
            return
        fg = user32.GetForegroundWindow()
        if fg and fg != hwnd:
            self._prev = fg
        self.expand(activate=True)

    def trace(self, what):
        """Временный журнал: кто и при каком положении мыши развернул/свернул панель (ищем баг с «Мышью без границ»)."""
        try:
            import inspect
            pt = wt.POINT(); user32.GetCursorPos(ctypes.byref(pt))
            caller = inspect.stack()[2].function
            fg = user32.GetForegroundWindow(); cls = ctypes.create_unicode_buffer(80)
            user32.GetClassNameW(fg, cls, 80)
            self.log(f"[{os.environ.get('COMPUTERNAME', '?')}] {what} из {caller}: было {self._state}, "
                     f"мышь {pt.x},{pt.y}, видна={cursor_visible()}, активное окно {cls.value}")
        except Exception:
            pass

    def expand(self, activate):
        """Выезжает в полный размер — из скрытого состояния или из шторки."""
        self.trace(f"РАЗВЕРНУЛАСЬ (activate={activate})")
        with self._lock:
            if self._dropping:
                return
            hwnd = self._hwnd()
            style_window(hwnd)
            if self._state == "hidden":
                self._mon = monitor_rect(cursor=True)   # на мониторе, где сейчас мышь
            w, h = self._panel_size()          # под размер и масштаб монитора
            x = self._mon.left + (self._mon.right - self._mon.left - w) // 2
            top = self._mon.top if NOTCH else self._mon.top + 10
            self._glass = capture_glass(hwnd, x, top, w, h)
            if NOTCH:
                self._notch_in(hwnd, x, top, w, h, activate)
                return
            clear_shape(hwnd)
            if self._state == "strip":
                r = rect_of(hwnd)
                slide(hwnd, r.left, r.top, self._mon.top - (r.bottom - r.top) - 4, 0.08, ease_in)
            start = top - h - 20
            place(hwnd, x, start, w, h)
            if self._state == "hidden":
                self._window.show()
                time.sleep(0.05)                # show() доезжает до окна не сразу и может сбить позицию
                place(hwnd, x, start, w, h)
            self._state = "open" if activate else "peek"
            self._had_focus = False
            if activate:
                user32.SetForegroundWindow(hwnd)
                focus_page(self._window) or focus_webview(hwnd)
            # обновить стекло и вкладку параллельно с анимацией (evaluate_js ждёт ответа страницы)
            threading.Thread(target=self._window.evaluate_js,
                             args=("window.onShown && window.onShown()",), daemon=True).start()
            slide(hwnd, x, start, top, SLIDE_IN, ease_out)

    def collapse(self):
        """Сворачивается в язычок сверху экрана."""
        self.trace("свернулась")
        with self._lock:
            if self._state not in ("open", "peek") or self._dropping:
                return
            hwnd = self._hwnd()
            r = rect_of(hwnd)
            x = self._mon.left + (self._mon.right - self._mon.left - STRIP_W) // 2
            if self._shrink(hwnd, r):
                self._strip_place(hwnd)
                self._state = "strip"
                self._js("window.notchDone && notchDone()")
                return
            slide(hwnd, r.left, r.top, self._mon.top - (r.bottom - r.top) - 20, SLIDE_OUT, ease_in)
            place(hwnd, x, self._mon.top - STRIP_H, STRIP_W, STRIP_H)
            self._state = "strip"
            slide(hwnd, x, self._mon.top - STRIP_H, self._mon.top, 0.12, ease_out)

    # ---------- живая шторка: форма под содержимое (зовёт ui.html → islTick) ----------

    # ---------- размеры под монитор: масштаб Windows (100 %, 125 %, 150 %…) и размер экрана ----------

    def _k(self):
        return monitor_scale(self._mon) if self._mon is not None else 1.0

    def _scale(self):
        return self._k()

    def _px(self, v):
        return int(round(v * self._k()))

    def _px3(self, a, b, c):
        return self._px(a), self._px(b), self._px(c)

    def _panel_size(self):
        """Панель — 760×560 «экранных» точек, но не шире половины экрана и не выше 82 % его высоты;
        умноженное на масштаб Windows — иначе на ноутбуке со 150 % панель выходит мелкой и тесной."""
        k = self._k()
        mw = (self._mon.right - self._mon.left) / k
        mh = (self._mon.bottom - self._mon.top) / k
        w = max(600, min(WIDTH, mw * 0.5))
        h = max(420, min(HEIGHT, mh * 0.82))
        return int(w * k), int(h * k)

    def _strip_place(self, hwnd):
        """Окно шторки — с запасом под расширение (до STRIP_WIN); видна только форма self._strip."""
        k = self._scale()
        ww, wh = int(STRIP_WIN_W * k), int(STRIP_WIN_H * k)
        x = self._mon.left + (self._mon.right - self._mon.left - ww) // 2
        self._strip_x0 = x
        self._strip_cur = [float(v) for v in self._strip] + [0.0]
        self._strip_v = [0.0, 0.0, 0.0, 0.0]
        set_shape(hwnd, ww, *self._strip, redraw=False)
        place(hwnd, x, self._mon.top, ww, wh)
        self._js("window.islEnter && islEnter()")

    def strip_shape(self, w, h, r=14):
        """Шторка меняет форму под содержимое — w, h, r в CSS-пикселях. Доводит её пружина (island_loop)."""
        k = self._scale()
        self._strip = [int(w * k), int(h * k), int(r * k)]
        return True

    def island_loop(self):
        """Пружина живой шторки, 60 раз в секунду: форма плавно доходит до нужной, с лёгким перелётом.
        Мышь рядом — шторка чуть подрастает и тянется к курсору, как магнит; ушла — возвращается."""
        dt_ = 1 / 60
        near_was = False
        while True:
            time.sleep(dt_)
            if self._state != "strip" or self._dropping or not self._lock.acquire(blocking=False):
                continue
            try:
                hwnd = self._hwnd()
                if not hwnd or self._state != "strip":
                    continue
                r = rect_of(hwnd)
                ww = r.right - r.left
                if self._strip_x0 is None:
                    self._strip_x0 = r.left
                k = self._scale()
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                cx = self._strip_x0 + ww // 2
                bw, bh, br = self._strip
                near = (abs(pt.x - cx) < bw / 2 + 70 * k and self._mon.top - 2 <= pt.y < self._mon.top + bh + 60 * k
                        and cursor_visible())
                if near != near_was:
                    near_was = near
                    self._js(f"window.islHover && islHover({'true' if near else 'false'})")
                target = [bw + (18 * k if near else 0), bh + (6 * k if near else 0), br + (2 * k if near else 0),
                          max(-16 * k, min(16 * k, (pt.x - cx) * 0.12)) if near else 0.0]
                moved = False
                for i in range(4):          # пружина: жёсткость 320, затухание 26 — чуть-чуть перелетает
                    x, v = self._strip_cur[i], self._strip_v[i]
                    v += (320 * (target[i] - x) - 26 * v) * dt_
                    x += v * dt_
                    if abs(target[i] - x) < 0.2 and abs(v) < 2:
                        x, v = target[i], 0.0
                    if abs(x - self._strip_cur[i]) > 0.05:
                        moved = True
                    self._strip_cur[i], self._strip_v[i] = x, v
                if moved:
                    w, h, rad, dx = self._strip_cur
                    set_shape(hwnd, ww, max(40, w), max(12, h), max(2, rad))
                    move(hwnd, self._strip_x0 + dx, self._mon.top)
            except Exception as e:
                self.log(f"island: {e!r}")
            finally:
                self._lock.release()

    def audio_level(self):
        """Громкость того, что сейчас звучит (0…1) — для эквалайзера в шторке. Читает её один свой поток
        (meter_loop): звуковые COM-объекты Windows, созданные и брошенные в разных потоках, роняли панель."""
        self._level_wanted = time.time()
        if not getattr(self, "_meter_started", False):
            self._meter_started = True
            threading.Thread(target=self.meter_loop, daemon=True).start()
        return getattr(self, "_level", 0.0)

    def meter_loop(self):
        import comtypes
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
        comtypes.CoInitialize()
        meter, made = None, 0.0
        while True:
            if time.time() - getattr(self, "_level_wanted", 0) > 3:     # эквалайзер не виден — не опрашиваем
                time.sleep(0.25)
                continue
            try:
                if meter is None or time.time() - made > 5:              # раз в 5 с — вдруг сменили наушники/динамики
                    dev = AudioUtilities.GetSpeakers()
                    raw = dev._dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
                    meter, made = raw.QueryInterface(IAudioMeterInformation), time.time()
                self._level = round(float(meter.GetPeakValue()), 3)
            except Exception:
                meter, self._level = None, 0.0
            time.sleep(0.05)

    def calendar(self):
        return calendar_state()

    def calendar_check(self):
        calendar_refresh(force=True)
        return calendar_state()

    def _notch_in(self, hwnd, x, top, w, h, activate):
        """Вырасти из шторки: окно сразу на месте и полного размера, видна только часть размером со шторку."""
        self._js("window.notchIn && notchIn()")          # содержимое проявится, когда форма почти раскроется
        start = tuple(int(v) for v in self._strip_cur[:3]) if self._state == "strip" else self._px3(STRIP_W, STRIP_H, STRIP_R)
        set_shape(hwnd, w, *start, redraw=False)
        place(hwnd, x, top, w, h)
        if self._state == "hidden":
            self._window.show()
            time.sleep(0.05)                    # show() доезжает до окна не сразу и может сбить позицию
            place(hwnd, x, top, w, h)
        self._state = "open" if activate else "peek"
        self._had_focus = False
        if activate:
            user32.SetForegroundWindow(hwnd)
            focus_page(self._window) or focus_webview(hwnd)
        self._js("window.onShown && window.onShown()")
        animate_shape(hwnd, w, start, (w, h, self._px(PANEL_R)), NOTCH_IN, grow=True)

    def watch(self):
        """Следит за фокусом и мышью: клик мимо — в шторку, наведение на шторку — развернуть.
        Развернуть только от настоящего наведения: курсор виден, пришёл к шторке сам (а не «прыгнул»)
        и задержался ~0,12 с. Иначе панель раскрывается, когда «Мышь без границ» уводит курсор
        на другой ноутбук и прячет его на этом экране."""
        away_since = None
        hover_since = None
        last = None
        parked = False
        while True:
            time.sleep(0.05)
            try:
                hwnd = self._hwnd()
                if not hwnd or self._state == "hidden" or self._dropping:   # тащат файл — форму ведёт полка
                    continue
                fg = user32.GetForegroundWindow()
                if fg and fg != hwnd:
                    self._prev = fg
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                r = rect_of(hwnd)
                inside = r.left - 12 <= pt.x <= r.right + 12 and r.top - 12 <= pt.y <= r.bottom + 12
                if self._state == "strip":            # окно шторки шире видимой формы — смотрим на форму
                    cx, sw, sh = (r.left + r.right) // 2, self._strip_cur[0], self._strip_cur[1]
                    inside = cx - sw // 2 - 12 <= pt.x <= cx + sw // 2 + 12 and r.top - 12 <= pt.y <= r.top + sh + 12
                # «Мышь без границ», уводя курсор на другой ноутбук, ставит его в верх по центру экрана —
                # ровно на шторку — и делает активным своё окно. После такого прыжка ждём, пока мышь
                # реально сдвинется рукой; пока активно окно «Мыши без границ» — не разворачиваемся вовсе.
                if last is not None:
                    step = abs(pt.x - last[0]) + abs(pt.y - last[1])
                    if step > 400:
                        parked = True
                    elif step > 1:
                        parked = False
                last = (pt.x, pt.y)
                real = inside and cursor_visible() and not parked and not is_mwb(fg)
                if self._state == "open":
                    if fg == hwnd:
                        self._had_focus = True
                    elif self._had_focus and not self._pinned and fg:
                        if os.environ.get("PANEL_DEBUG"):
                            buf, cls = ctypes.create_unicode_buffer(120), ctypes.create_unicode_buffer(120)
                            user32.GetWindowTextW(fg, buf, 120); user32.GetClassNameW(fg, cls, 120)
                            pid = wt.DWORD(); user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
                            self.log(f"collapse: fg={fg} '{buf.value}' {cls.value} pid={pid.value} me={os.getpid()}")
                        self.collapse()
                elif self._state == "strip":
                    if not real:
                        hover_since = None
                    elif hover_since is None:
                        hover_since = time.perf_counter()
                    elif time.perf_counter() - hover_since > 0.12:
                        hover_since = None
                        self.expand(activate=False)
                elif self._state == "peek":
                    if fg == hwnd:
                        self._state, self._had_focus = "open", True
                    elif inside and cursor_visible():
                        away_since = None
                    else:
                        away_since = away_since or time.perf_counter()
                        if time.perf_counter() - away_since > 0.45:
                            away_since = None
                            self.collapse()
            except Exception as e:
                self.log(f"watch: {e!r}")


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hCursor", wt.HANDLE), ("ptScreenPos", wt.POINT)]


kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


def is_mwb(hwnd):
    """Активное окно принадлежит «Мыши без границ» (PowerToys) — она перехватывает мышь для другого ноутбука."""
    if not hwnd:
        return False
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(512)
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return bool(ok) and "mousewithoutborders" in buf.value.lower()
    finally:
        kernel32.CloseHandle(h)


def cursor_visible():
    """Курсор показан на этом компьютере. «Мышь без границ» прячет его, когда управляешь другим ноутбуком."""
    ci = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
    if not user32.GetCursorInfo(ctypes.byref(ci)):
        return True
    return bool(ci.flags & 1)   # CURSOR_SHOWING


SLIDE_IN, SLIDE_OUT = 0.22, 0.14   # секунды
NOTCH = True                       # «вырез» у верхнего края, растёт из шторки (False — старое выезжание сверху)
NOTCH_IN, NOTCH_OUT = 0.42, 0.24   # секунды: раскрыться / сжаться в шторку
STRIP_W, STRIP_H = 320, 34         # язычок-шторка (до первой команды живой шторки)
STRIP_WIN_W, STRIP_WIN_H = 480, 96 # окно шторки с запасом: форма внутри растёт до этого размера
ISLAND_MORPH = 0.42                # смена формы живой шторки, с пружинкой
STRIP_R, PANEL_R = 14, 20          # радиус нижних углов шторки и панели
DROP_W, DROP_H, DROP_R = 380, 70, 18   # шторка-приёмник полки «Положить на полку»
DROP_BAND_H = 124                  # до какой высоты сжимается открытая панель, когда к ней тащат файл
DROP_ANIM = 0.26


def monitor_scale(r):
    """Масштаб Windows на мониторе с этим прямоугольником: 1.0 = 100 %, 1.5 = 150 %."""
    try:
        hm = user32.MonitorFromPoint(wt.POINT((r.left + r.right) // 2, (r.top + r.bottom) // 2), 2)
        x, y = ctypes.c_uint(), ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(hm, 0, ctypes.byref(x), ctypes.byref(y))
        return (x.value or 96) / 96
    except Exception:
        return 1.0


def ease_out(t):
    return 1 - (1 - t) ** 3


def ease_in(t):
    return t ** 3


def ease_out_quint(t):
    return 1 - (1 - t) ** 5


def ease_in_out(t):
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


gdi32 = ctypes.windll.gdi32
gdi32.CreateRoundRectRgn.restype = wt.HRGN
gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
user32.SetWindowRgn.argtypes = [wt.HWND, wt.HRGN, wt.BOOL]


def set_shape(hwnd, win_w, w, h, radius, redraw=True):
    """Видимая часть окна — w×h по центру у верхнего края, нижние углы скруглены, верхние прямые
    (скругление уходит выше окна). Регион окно забирает себе — удалять его не нужно."""
    left = (win_w - w) // 2
    d = max(2, int(radius) * 2)
    rgn = gdi32.CreateRoundRectRgn(int(left), -d, int(left + w) + 1, int(h) + 1, d, d)
    user32.SetWindowRgn(hwnd, rgn, bool(redraw))


def clear_shape(hwnd):
    user32.SetWindowRgn(hwnd, None, True)


def ease_out_back(t, k=1.25):
    t -= 1
    return 1 + (k + 1) * t ** 3 + k * t ** 2


def animate_shape(hwnd, win_w, a, b, duration, grow=True, spring=False):
    """Плавно меняет видимую часть окна от a к b — (ширина, высота, радиус).
    Как у Dynamic Island: раскрываясь, сначала тянется в ширину, потом вниз; сворачиваясь — наоборот."""
    def part(t, s, e):
        return min(1.0, max(0.0, (t - s) / (e - s)))
    ease = ease_out if grow else ease_in_out
    t0 = time.perf_counter()
    frames = 0
    while True:
        frames += 1
        t = min(1.0, (time.perf_counter() - t0) / duration)
        if spring:                                  # живая шторка: обе стороны сразу, с лёгким перелётом
            kw = kh = ease_out_back(t)
        else:
            kw = ease(part(t, 0.0, 0.7) if grow else part(t, 0.3, 1.0))
            kh = ease(part(t, 0.12, 1.0) if grow else part(t, 0.0, 0.8))
        set_shape(hwnd, win_w, a[0] + (b[0] - a[0]) * kw, a[1] + (b[1] - a[1]) * kh, a[2] + (b[2] - a[2]) * kh)
        if t >= 1.0:
            return frames
        time.sleep(0.005)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def monitor_rect(cursor=True):
    """Рабочая область монитора под мышью (или основного), в физических пикселях."""
    pt = wt.POINT()
    if cursor:
        user32.GetCursorPos(ctypes.byref(pt))
    mon = user32.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(mon, ctypes.byref(info))
    return info.rcWork


def rect_of(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def place(hwnd, x, y, w, h):
    user32.SetWindowPos(hwnd, wt.HWND(-1), int(x), int(y), int(w), int(h), 0x0010)  # TOPMOST, NOACTIVATE


def move(hwnd, x, y):
    user32.SetWindowPos(hwnd, wt.HWND(-1), int(x), int(y), 0, 0, 0x0001 | 0x0010)  # + NOSIZE


def slide(hwnd, x, y0, y1, duration, ease):
    t0 = time.perf_counter()
    while True:
        t = min(1.0, (time.perf_counter() - t0) / duration)
        move(hwnd, x, y0 + (y1 - y0) * ease(t))
        if t >= 1.0:
            break
        time.sleep(0.008)


def focus_page(window):
    """Отдать клавиатуру странице внутри окна. pywebview делает это только при самом первом показе
    (on_shown → webview.Focus()), а мы окно прячем и показываем снова — поэтому повторяем сами,
    в потоке окна: Activate() + WebView2.Focus() (он же MoveFocus у контроллера WebView2)."""
    form = getattr(window, "native", None)
    if form is None:
        return False
    from System import Func, Type

    def run():
        form.Activate()
        form.browser.webview.Focus()
        return None

    try:
        form.Invoke(Func[Type](run))
        return True
    except Exception:
        return False


def focus_webview(hwnd):
    """Запасной путь: окно отрисовки Chromium + SetFocus через AttachThreadInput."""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(child, _):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(child, cls, 64)
        if cls.value == "Chrome_RenderWidgetHostHWND":
            found.append(child)
            return False
        return True

    user32.EnumChildWindows(wt.HWND(hwnd), cb, 0)
    if not found:
        return False
    target_tid = user32.GetWindowThreadProcessId(wt.HWND(found[0]), None)
    my_tid = kernel32.GetCurrentThreadId()
    attached = user32.AttachThreadInput(my_tid, target_tid, True)
    try:
        user32.SetFocus(wt.HWND(found[0]))
    finally:
        if attached:
            user32.AttachThreadInput(my_tid, target_tid, False)
    return True


def capture_glass(hwnd, x, y, w, h):
    """Снимок экрана под окном → сильное размытие → data-URI для фона-стекла.
    Своё окно на время снимка исключаем из захвата (WDA_EXCLUDEFROMCAPTURE), иначе в снимок попадёт
    сама шторка. В скриншотах и записи экрана панель после этого видна как обычно."""
    excluded = bool(hwnd) and bool(user32.SetWindowDisplayAffinity(wt.HWND(hwnd), 0x11))
    try:
        if excluded:
            time.sleep(0.035)                   # исключение срабатывает со следующего кадра
        from PIL import ImageFilter, ImageGrab
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).convert("RGB")
        small = img.resize((max(1, w // 6), max(1, h // 6)))
        small = small.filter(ImageFilter.GaussianBlur(7))
        buf = io.BytesIO()
        small.resize((w // 2, h // 2)).save(buf, "JPEG", quality=80)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None
    finally:
        if excluded:
            user32.SetWindowDisplayAffinity(wt.HWND(hwnd), 0)


def style_window(hwnd):
    """Без кнопки на панели задач. В режиме выреза форму задаёт регион окна, поэтому скругление, рамку
    и тень Windows 11 выключаем — иначе они рисуются вокруг всего прямоугольника окна («контур»).
    Без выреза — обычные скруглённые углы Windows 11."""
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
    ex = user32.GetWindowLongPtrW(wt.HWND(hwnd), GWL_EXSTYLE)
    want = (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    if ex != want:
        user32.SetWindowLongPtrW(wt.HWND(hwnd), GWL_EXSTYLE, want)
    dwm = ctypes.windll.dwmapi
    # 2 — NCRENDERING_POLICY (1 = выключить рамку/тень), 33 — CORNER (1 = без скругления, 2 = скруглить), 34 — BORDER
    attrs = ((2, 1), (33, 1), (34, 0xFFFFFFFE)) if NOTCH else ((33, 2), (34, 0xFFFFFFFE))
    for attr, val in attrs:
        v = ctypes.c_uint(val)
        dwm.DwmSetWindowAttribute(wt.HWND(hwnd), wt.DWORD(attr), ctypes.byref(v), wt.DWORD(ctypes.sizeof(v)))


def hotkey_loop(api: Api):
    if not user32.RegisterHotKey(None, 1, HOTKEY_MOD, HOTKEY_VK):
        ctypes.windll.user32.MessageBoxW(
            None, "Alt+Пробел уже занята другой программой. Поменяй HOTKEY_VK в panel.py.", TITLE, 0x30)
        return
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == 0x0312:  # WM_HOTKEY
            try:
                api.toggle()
            except Exception as e:
                api.log(f"toggle: {e!r}")


def first_run_open(api):
    """Новый человек: при первом запуске панель сама выезжает с приветствием — иначе он её не найдёт."""
    if settings().get("onboarded"):
        return
    for _ in range(60):
        time.sleep(0.25)
        if api._hwnd():
            break
    time.sleep(2.0)                     # страница успевает загрузиться
    try:
        api.expand(activate=True)
    except Exception as e:
        api.log(f"первый запуск: {e!r}")


def main():
    if "--overlay" in sys.argv:          # программа (.exe) запускает рисование поверх экрана сама собой
        import overlay
        overlay.main()
        return
    # Per-Monitor V2: координаты и размеры — настоящие пиксели каждого монитора, WebView сам рисует
    # в его масштабе. Ставим до создания окон (pywebview потом пробует SetProcessDPIAware — уже не сработает).
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    # одна копия панели
    kernel32.CreateMutexW(None, False, "Local\\MyPanel.Panel")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return
    # маркер поверх экрана (Alt+S) — отдельный процесс, у него свой Tk; второй копии он сам не даст запуститься
    try:
        import subprocess
        pyw = Path(sys.executable).with_name("pythonw.exe")
        helpers = [h for h in ["overlay.py"] if (HERE / h).exists()]   # рисование (Alt+S)
        # голосовой помощник (если лежит рядом) — кроме компьютеров из settings.json → jarvis.autostart_off
        try:
            off = json.loads(SETTINGS.read_text(encoding="utf-8")).get("jarvis", {}).get("autostart_off", [])
        except Exception:
            off = []
        if (HERE / "jarvis.py").exists() and os.environ.get("COMPUTERNAME", "").upper() not in [c.upper() for c in off]:
            helpers.append("jarvis.py")
        if FROZEN:                                  # в .exe рисование — тот же .exe с ключом, Джарвиса нет
            subprocess.Popen([sys.executable, "--overlay"])
            helpers = []
        for helper in helpers:
            subprocess.Popen([str(pyw if pyw.exists() else sys.executable), str(HERE / helper)], cwd=HERE)
    except Exception:
        pass
    ensure_data()
    api = Api()
    api._window = webview.create_window(
        TITLE, str(HERE / "ui.html"), js_api=api,
        width=WIDTH, height=HEIGHT, frameless=True, on_top=True, hidden=True,
        resizable=False, easy_drag=False, min_size=(120, 24), background_color="#e9ebf1",
    )
    # полка для файлов живёт внутри панели: файл тащат к краю — принимает сама панель (на Honor не нужна, там SuperHub)
    try:
        import shelf
        shelf.start_in_panel(api)
    except Exception as e:
        api.log(f"полка: {e!r}")
    threading.Thread(target=hotkey_loop, args=(api,), daemon=True).start()
    threading.Thread(target=api.watch, daemon=True).start()
    threading.Thread(target=api.island_loop, daemon=True).start()
    threading.Thread(target=timer_loop, daemon=True).start()
    # служебные данные окна — локально на каждом компьютере, не в OneDrive (иначе два ноутбука их портят)
    storage = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MyPanel" / "webview"
    # ui.html отдаётся с одного и того же адреса, и WebView держит старую копию в кэше — после правок
    # панель показывала прежний интерфейс. Кэш страниц чистим при каждом запуске (настройки в Local Storage не трогаем).
    import shutil
    for cache in ("Cache", "Code Cache"):
        shutil.rmtree(storage / "EBWebView" / "Default" / cache, ignore_errors=True)
    threading.Thread(target=first_run_open, args=(api,), daemon=True).start()
    threading.Thread(target=update_loop, args=(api,), daemon=True).start()
    webview.start(private_mode=False, storage_path=str(storage))


if __name__ == "__main__":
    main()
