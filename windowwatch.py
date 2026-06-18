"""
WindowWatch  -  W.01.01
A Windows tray/panel utility that flags activity in background windows.

Two detection methods run together:
  1. FLASH detection  - listens for the OS "flash taskbar" signal (SHELLHOOK).
                        Cheap and reliable for apps that request attention
                        (Slack, Teams, Discord, etc). Works even when hidden.
  2. PIXEL detection   - captures a hidden window's pixels via PrintWindow and
                        compares successive frames. Catches changes that don't
                        flash the taskbar (e.g. a new browser chat response).

Audio cue fires ONLY when the changed window is hidden / in the background.
Visual flag is a small always-on-top status panel listing watched windows.

Windows only. Requires: pywin32, Pillow.
"""

import sys
import time
import threading
import hashlib
import ctypes
from ctypes import wintypes

try:
    import win32gui
    import win32con
    import win32api
    import win32ui
    import win32process
except ImportError:
    print("This program requires pywin32.  Install with:  pip install pywin32")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("This program requires Pillow.  Install with:  pip install Pillow")
    sys.exit(1)

import tkinter as tk
from tkinter import ttk
import winsound

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

# ----------------------------------------------------------------------------
# Configuration defaults
# ----------------------------------------------------------------------------
PIXEL_POLL_SECONDS = 1.5        # how often pixel-watched windows are captured
PIXEL_DIFF_THRESHOLD = 0.004    # fraction of changed bytes to count as "activity"
AUDIO_FILE = None               # path to a .wav, or None for the default beep
AUDIO_COOLDOWN_SECONDS = 4      # min gap between repeated alerts per window


# ----------------------------------------------------------------------------
# Helpers for window state
# ----------------------------------------------------------------------------
def is_window_hidden(hwnd):
    """
    Return True when the window is NOT the active foreground window and is
    either minimized or fully/partly obscured. We treat 'not foreground' as
    'background' for alerting purposes, which matches the user's intent: alert
    when the window they're not looking at has activity.
    """
    if not win32gui.IsWindow(hwnd):
        return False
    if win32gui.IsIconic(hwnd):            # minimized
        return True
    foreground = win32gui.GetForegroundWindow()
    return hwnd != foreground


def window_title(hwnd):
    try:
        return win32gui.GetWindowText(hwnd)
    except Exception:
        return "<unknown>"


def enum_visible_windows():
    """Return list of (hwnd, title) for top-level windows with a title."""
    results = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        # skip tool windows / our own panel
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex & win32con.WS_EX_TOOLWINDOW:
            return
        results.append((hwnd, title))

    win32gui.EnumWindows(_cb, None)
    return results


# ----------------------------------------------------------------------------
# Pixel capture of a (possibly hidden) window via PrintWindow
# ----------------------------------------------------------------------------
PW_RENDERFULLCONTENT = 0x00000002  # needed for many modern / GPU windows


def capture_window_hash(hwnd):
    """
    Render the window to an offscreen bitmap and return a hash of its pixels.
    Returns None if capture fails (some GPU-accelerated windows return blank).
    """
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        w, h = right - left, bottom - top
        if w <= 0 or h <= 0:
            return None

        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        save_bmp = win32ui.CreateBitmap()
        save_bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(save_bmp)

        result = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(),
                                    PW_RENDERFULLCONTENT)

        bmpinfo = save_bmp.GetInfo()
        bmpstr = save_bmp.GetBitmapBits(True)

        # cleanup
        win32gui.DeleteObject(save_bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)

        if not result:
            return None

        img = Image.frombuffer(
            "RGB",
            (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
            bmpstr, "raw", "BGRX", 0, 1,
        )
        # downscale so tiny cursor blinks / antialiasing don't trip the diff
        img = img.resize((160, 120))
        return img.tobytes()
    except Exception:
        return None


def byte_diff_fraction(a, b):
    """Fraction of differing bytes between two equal-length byte strings."""
    if a is None or b is None or len(a) != len(b):
        return 1.0
    # sample every 4th byte for speed; good enough for change detection
    diff = sum(1 for i in range(0, len(a), 4) if a[i] != b[i])
    return diff / (len(a) / 4)


# ----------------------------------------------------------------------------
# The set of watched windows and their state
# ----------------------------------------------------------------------------
class Watched:
    def __init__(self, hwnd, title, pixel_watch=False):
        self.hwnd = hwnd
        self.title = title
        self.pixel_watch = pixel_watch
        self.last_hash = None
        self.flagged = False           # has unseen activity
        self.last_alert = 0.0

    def alive(self):
        return win32gui.IsWindow(self.hwnd)


class Monitor:
    def __init__(self):
        self.watched = {}              # hwnd -> Watched
        self.lock = threading.Lock()
        self.running = True

    def add(self, hwnd, title, pixel_watch=False):
        with self.lock:
            self.watched[hwnd] = Watched(hwnd, title, pixel_watch)

    def remove(self, hwnd):
        with self.lock:
            self.watched.pop(hwnd, None)

    def clear_flag(self, hwnd):
        with self.lock:
            w = self.watched.get(hwnd)
            if w:
                w.flagged = False

    def fire_activity(self, hwnd):
        """Called when activity is detected on a watched window."""
        now = time.time()
        with self.lock:
            w = self.watched.get(hwnd)
            if not w:
                return
            w.flagged = True
            hidden = is_window_hidden(hwnd)
            if hidden and (now - w.last_alert) > AUDIO_COOLDOWN_SECONDS:
                w.last_alert = now
                self._play_sound()

    @staticmethod
    def _play_sound():
        try:
            if AUDIO_FILE:
                winsound.PlaySound(AUDIO_FILE,
                                   winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

    # ---- pixel polling loop (runs in its own thread) ----
    def pixel_loop(self):
        while self.running:
            with self.lock:
                targets = [w for w in self.watched.values()
                           if w.pixel_watch and w.alive()]
            for w in targets:
                # only bother capturing when the window is hidden; if the user
                # is looking at it, they don't need an alert
                if not is_window_hidden(w.hwnd):
                    continue
                h = capture_window_hash(w.hwnd)
                if h is None:
                    continue
                if w.last_hash is not None:
                    frac = byte_diff_fraction(w.last_hash, h)
                    if frac > PIXEL_DIFF_THRESHOLD:
                        self.fire_activity(w.hwnd)
                w.last_hash = h
            time.sleep(PIXEL_POLL_SECONDS)

    # ---- prune dead windows ----
    def prune_loop(self):
        while self.running:
            with self.lock:
                dead = [h for h, w in self.watched.items() if not w.alive()]
                for h in dead:
                    self.watched.pop(h, None)
            time.sleep(3)

    # ---- live title refresh + title-change-as-activity ----
    def title_loop(self):
        """
        Poll each watched window's current title. Update the stored title so
        the panel shows the live value, and treat a title change on a HIDDEN
        window as activity. Many chat sites bump the title (e.g. add "(1)")
        when a reply arrives, so this catches hidden Edge tabs even when pixel
        capture returns a blank frame.
        """
        while self.running:
            with self.lock:
                targets = [w for w in self.watched.values() if w.alive()]
            for w in targets:
                current = window_title(w.hwnd)
                if current and current != w.title:
                    changed_while_hidden = is_window_hidden(w.hwnd)
                    with self.lock:
                        w.title = current
                    if changed_while_hidden:
                        self.fire_activity(w.hwnd)
            time.sleep(1.0)


# ----------------------------------------------------------------------------
# Shell hook listener for taskbar-flash (HSHELL_FLASH) events
# ----------------------------------------------------------------------------
class FlashListener(threading.Thread):
    """
    Creates a hidden message-only window, registers as a shell hook, and
    receives HSHELL_FLASH / HSHELL_REDRAW notifications. These fire when an
    app requests user attention (the orange taskbar flash) even while hidden.
    """
    HSHELL_FLASH = 0x8006
    HSHELL_REDRAW = 6  # also sent on title changes; treated as soft activity

    def __init__(self, monitor):
        super().__init__(daemon=True)
        self.monitor = monitor
        self.hwnd = None

    def run(self):
        msg_flash = user32.RegisterWindowMessageW("SHELLHOOK")

        WNDPROCTYPE = ctypes.WINFUNCTYPE(
            ctypes.c_long, wintypes.HWND, ctypes.c_uint,
            wintypes.WPARAM, wintypes.LPARAM)

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == msg_flash:
                code = wparam & 0x7FFF
                if code in (self.HSHELL_FLASH, self.HSHELL_REDRAW):
                    target = lparam
                    with self.monitor.lock:
                        watched = target in self.monitor.watched
                    if watched:
                        self.monitor.fire_activity(target)
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROCTYPE(wndproc)  # keep ref alive

        class WNDCLASS(ctypes.Structure):
            _fields_ = [
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROCTYPE),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        hinst = win32api.GetModuleHandle(None)
        wc = WNDCLASS()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "WindowWatchHook"
        atom = user32.RegisterClassW(ctypes.byref(wc))

        self.hwnd = user32.CreateWindowExW(
            0, atom, "WindowWatchHook", 0, 0, 0, 0, 0,
            None, None, hinst, None)

        user32.RegisterShellHookWindow(self.hwnd)

        msg = wintypes.MSG()
        while self.monitor.running:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


# ----------------------------------------------------------------------------
# The always-on-top status panel (Tkinter)
# ----------------------------------------------------------------------------
class Panel:
    def __init__(self, monitor):
        self.monitor = monitor
        self.root = tk.Tk()
        self.root.title("WindowWatch")
        self.root.attributes("-topmost", True)
        self.root.geometry("340x260+40+40")
        self.root.configure(bg="#1e1e1e")

        self.rows = {}  # hwnd -> (frame, label, button)

        header = tk.Frame(self.root, bg="#1e1e1e")
        header.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(header, text="WindowWatch", fg="#ddd", bg="#1e1e1e",
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        tk.Button(header, text="+ Add window", command=self.open_add_dialog,
                  bg="#3a3a3a", fg="#fff", relief="flat",
                  font=("Segoe UI", 9)).pack(side="right")

        self.list_frame = tk.Frame(self.root, bg="#1e1e1e")
        self.list_frame.pack(fill="both", expand=True, padx=8, pady=4)

        tk.Label(self.root,
                 text="● flagged = activity while hidden.  Click to clear.",
                 fg="#888", bg="#1e1e1e",
                 font=("Segoe UI", 8)).pack(side="bottom", pady=4)

        self.refresh()

    def open_add_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Add a window to watch")
        dlg.attributes("-topmost", True)
        dlg.geometry("460x340+80+80")
        dlg.configure(bg="#1e1e1e")

        tk.Label(dlg, text="Pick a window:", fg="#ddd", bg="#1e1e1e",
                 font=("Segoe UI", 10)).pack(anchor="w", padx=10, pady=(10, 4))

        listbox = tk.Listbox(dlg, height=12, bg="#2a2a2a", fg="#eee",
                             selectbackground="#0a64a4",
                             font=("Segoe UI", 9))
        listbox.pack(fill="both", expand=True, padx=10, pady=4)

        windows = enum_visible_windows()
        for hwnd, title in windows:
            listbox.insert("end", f"{title}   [{hwnd}]")

        pixel_var = tk.IntVar(value=1)
        tk.Checkbutton(
            dlg,
            text="Also pixel-watch (for browser chat tabs that don't flash)",
            variable=pixel_var, fg="#ccc", bg="#1e1e1e",
            selectcolor="#1e1e1e", activebackground="#1e1e1e",
            font=("Segoe UI", 9)).pack(anchor="w", padx=10, pady=4)

        def do_add():
            sel = listbox.curselection()
            if not sel:
                return
            hwnd, title = windows[sel[0]]
            self.monitor.add(hwnd, title, pixel_watch=bool(pixel_var.get()))
            dlg.destroy()
            self.refresh()

        tk.Button(dlg, text="Watch this window", command=do_add,
                  bg="#0a64a4", fg="#fff", relief="flat",
                  font=("Segoe UI", 10)).pack(pady=8)

    def refresh(self):
        # rebuild rows
        for child in self.list_frame.winfo_children():
            child.destroy()
        self.rows.clear()

        with self.monitor.lock:
            items = list(self.monitor.watched.values())

        if not items:
            tk.Label(self.list_frame,
                     text="No windows watched yet.\nClick '+ Add window'.",
                     fg="#777", bg="#1e1e1e",
                     font=("Segoe UI", 9)).pack(pady=20)
        else:
            for w in items:
                row = tk.Frame(self.list_frame, bg="#262626")
                row.pack(fill="x", pady=2)

                dot = "●" if w.flagged else "○"
                color = "#ff7b00" if w.flagged else "#4caf50"
                badge = "P" if w.pixel_watch else "F"

                lbl = tk.Label(
                    row,
                    text=f"{dot}  {w.title[:30]}",
                    fg=color, bg="#262626", anchor="w",
                    font=("Segoe UI", 9))
                lbl.pack(side="left", fill="x", expand=True, padx=6, pady=4)
                lbl.bind("<Button-1>",
                         lambda e, h=w.hwnd: self._clear(h))

                tk.Label(row, text=badge, fg="#888", bg="#262626",
                         font=("Segoe UI", 8)).pack(side="right", padx=2)
                tk.Button(row, text="✕", command=lambda h=w.hwnd: self._del(h),
                          bg="#262626", fg="#999", relief="flat",
                          font=("Segoe UI", 8)).pack(side="right")

        # schedule next refresh
        self.root.after(700, self.refresh)

    def _clear(self, hwnd):
        self.monitor.clear_flag(hwnd)

    def _del(self, hwnd):
        self.monitor.remove(hwnd)
        self.refresh()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.mainloop()

    def on_close(self):
        self.monitor.running = False
        self.root.destroy()


# ----------------------------------------------------------------------------
def main():
    monitor = Monitor()

    flash = FlashListener(monitor)
    flash.start()

    threading.Thread(target=monitor.pixel_loop, daemon=True).start()
    threading.Thread(target=monitor.prune_loop, daemon=True).start()
    threading.Thread(target=monitor.title_loop, daemon=True).start()

    panel = Panel(monitor)
    panel.run()


if __name__ == "__main__":
    main()
