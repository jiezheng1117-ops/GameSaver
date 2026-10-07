"""Gamesaver - automatic game save backup and restore for Windows.

Each game has a save location (folder or single file), a backup folder, and an
optional interval. Backups are timestamped zip files. One game is the active
game: the global hotkey and the backup/restore buttons act on it. Standard
library only.
"""

import ctypes
import hashlib
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import tkinter as tk
import uuid
import winreg
import winsound
import zipfile
from ctypes import wintypes
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Gamesaver"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_HOTKEY = "Ctrl+Alt+F5"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

BACKUP_RE = re.compile(r"^(\d{4}-\d\d-\d\d)_(\d\d)-(\d\d)-(\d\d)_(auto|manual|prerestore)\.zip$")
KIND_LABELS = {"auto": "Automatic", "manual": "Manual", "prerestore": "Before restore"}


# ---------------------------------------------------------------- backup core

class BackupError(Exception):
    pass


def safe_name(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .") or "game"


def backup_dir(game):
    return os.path.join(game["dest"], safe_name(game["name"]))


def list_files(src):
    """Return sorted (full path, path inside zip) pairs for a save location."""
    if os.path.isfile(src):
        return [(src, os.path.basename(src))]
    out = []
    for root, _dirs, files in os.walk(src):
        for f in files:
            full = os.path.join(root, f)
            out.append((full, os.path.relpath(full, src)))
    return sorted(out)


def signature(files):
    h = hashlib.sha1()
    for full, rel in files:
        try:
            st = os.stat(full)
        except OSError:
            continue
        h.update(f"{rel}|{st.st_size}|{st.st_mtime_ns}\n".encode("utf-8"))
    return h.hexdigest()


def list_backups(game):
    """Return [(path, when text, kind, size)] newest first."""
    d = backup_dir(game)
    out = []
    if os.path.isdir(d):
        for f in os.listdir(d):
            m = BACKUP_RE.match(f)
            if m:
                path = os.path.join(d, f)
                when = f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}"
                out.append((path, when, m.group(5), os.path.getsize(path)))
    out.sort(key=lambda b: os.path.basename(b[0]), reverse=True)
    return out


def do_backup(game, kind):
    """Zip the save location. Returns (zip path, signature), or None when an
    automatic backup is skipped because nothing changed."""
    src = game["src"]
    if not os.path.exists(src):
        raise BackupError(f"save location not found: {src}")
    files = list_files(src)
    if not files:
        raise BackupError("save location is empty")
    sig = signature(files)
    if kind == "auto" and sig == game.get("last_sig"):
        return None

    d = backup_dir(game)
    os.makedirs(d, exist_ok=True)
    final = os.path.join(d, time.strftime("%Y-%m-%d_%H-%M-%S") + f"_{kind}.zip")
    part = final + ".part"
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
            zf.comment = b"gamesaver:file" if os.path.isfile(src) else b"gamesaver:dir"
            for full, rel in files:
                # The game may hold a file open while writing it; retry briefly.
                for attempt in range(4):
                    try:
                        zf.write(full, rel)
                        break
                    except FileNotFoundError:
                        break
                    except OSError as e:
                        if attempt == 3:
                            raise BackupError(f"could not read {rel}: {e}")
                        time.sleep(0.5)
        os.replace(part, final)
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise

    keep = int(game.get("keep", 0))
    if keep > 0:
        autos = [b for b in list_backups(game) if b[2] == "auto"]
        for path, *_ in autos[keep:]:
            try:
                os.remove(path)
            except OSError:
                pass
    return final, sig


def do_restore(game, zip_path):
    """Replace the save location with the contents of a backup. The current
    save is backed up first. Returns the signature of the restored save."""
    src = game["src"]
    if os.path.exists(src) and list_files(src):
        do_backup(game, "prerestore")

    with zipfile.ZipFile(zip_path) as zf:
        if zf.testzip() is not None:
            raise BackupError("backup file is corrupted")
        if zf.comment == b"gamesaver:file":
            target = os.path.dirname(src)
        else:
            target = src
            if os.path.isdir(src):
                for entry in os.listdir(src):
                    p = os.path.join(src, entry)
                    if os.path.isdir(p) and not os.path.islink(p):
                        shutil.rmtree(p)
                    else:
                        os.remove(p)
        os.makedirs(target, exist_ok=True)
        for info in zf.infolist():
            out = zf.extract(info, target)
            if not info.is_dir():
                mtime = time.mktime(info.date_time + (0, 0, -1))
                os.utime(out, (mtime, mtime))
    return signature(list_files(src))


# -------------------------------------------------------------- global hotkey

MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
MODIFIERS = {"alt": 1, "ctrl": 2, "control": 2, "shift": 4, "win": 8}
NAMED_KEYS = {
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24, "insert": 0x2D,
    "delete": 0x2E, "pause": 0x13, "scrolllock": 0x91, "printscreen": 0x2C,
}


def parse_hotkey(text):
    """'Ctrl+Alt+F5' -> (modifier flags, virtual key). Raises ValueError."""
    mods, vk = 0, None
    for p in (p.strip().lower() for p in text.split("+")):
        if p in MODIFIERS:
            mods |= MODIFIERS[p]
        elif vk is not None or not p:
            raise ValueError("use a form like Ctrl+Alt+F5")
        elif re.fullmatch(r"f([1-9]|1\d|2[0-4])", p):
            vk = 0x70 + int(p[1:]) - 1
        elif len(p) == 1 and p.isascii() and p.isalnum():
            vk = ord(p.upper())
        elif p in NAMED_KEYS:
            vk = NAMED_KEYS[p]
        else:
            raise ValueError(f"unknown key '{p}'")
    if vk is None:
        raise ValueError("no key given (use a form like Ctrl+Alt+F5)")
    return mods, vk


class HotkeyThread(threading.Thread):
    """Owns the RegisterHotKey registration; it must be registered and polled
    on the same thread."""

    def __init__(self, on_fire, on_status):
        super().__init__(daemon=True)
        self.on_fire = on_fire
        self.on_status = on_status
        self._wanted = None
        self._dirty = False

    def set(self, combo):
        self._wanted = combo
        self._dirty = True

    def run(self):
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        registered = False
        while True:
            if self._dirty:
                self._dirty = False
                wanted = self._wanted
                if registered:
                    user32.UnregisterHotKey(None, 1)
                    registered = False
                if wanted:
                    registered = bool(user32.RegisterHotKey(None, 1, wanted[0] | MOD_NOREPEAT, wanted[1]))
                    self.on_status(registered)
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == WM_HOTKEY:
                    self.on_fire()
            time.sleep(0.05)


# -------------------------------------------------------------------- startup

def startup_command():
    pythonw = shutil.which("pythonw") or os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pythonw}" "{os.path.abspath(__file__)}" --minimized'


def startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except OSError:
        return False


def set_startup(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ------------------------------------------------------------------------- UI

def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class GameDialog(tk.Toplevel):
    def __init__(self, parent, game=None, default_dest=""):
        super().__init__(parent)
        self.title("Edit game" if game else "Add game")
        self.resizable(False, False)
        self.transient(parent)
        self.result = None
        game = game or {}

        self.name = tk.StringVar(value=game.get("name", ""))
        self.src = tk.StringVar(value=game.get("src", ""))
        self.dest = tk.StringVar(value=game.get("dest", default_dest))
        self.interval = tk.StringVar(value=str(game.get("interval", 15)))
        self.keep = tk.StringVar(value=str(game.get("keep", 20)))

        f = ttk.Frame(self, padding=12)
        f.grid()
        ttk.Label(f, text="Game name").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.name, width=60).grid(row=0, column=1, sticky="we", padx=6)

        ttk.Label(f, text="Save location").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.src, width=60).grid(row=1, column=1, sticky="we", padx=6)
        ttk.Button(f, text="Folder…", command=self.pick_src_dir).grid(row=1, column=2)
        ttk.Button(f, text="File…", command=self.pick_src_file).grid(row=1, column=3, padx=(4, 0))

        ttk.Label(f, text="Back up to").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.dest, width=60).grid(row=2, column=1, sticky="we", padx=6)
        ttk.Button(f, text="Folder…", command=self.pick_dest).grid(row=2, column=2)

        ttk.Label(f, text="Back up every").grid(row=3, column=0, sticky="w", pady=4)
        row = ttk.Frame(f)
        row.grid(row=3, column=1, sticky="w", padx=6)
        ttk.Entry(row, textvariable=self.interval, width=6).pack(side="left")
        ttk.Label(row, text=" minutes (0 = manual only; unchanged saves are skipped)").pack(side="left")

        ttk.Label(f, text="Keep last").grid(row=4, column=0, sticky="w", pady=4)
        row = ttk.Frame(f)
        row.grid(row=4, column=1, sticky="w", padx=6)
        ttk.Entry(row, textvariable=self.keep, width=6).pack(side="left")
        ttk.Label(row, text=" automatic backups (0 = keep all; manual backups are never deleted)").pack(side="left")

        btns = ttk.Frame(f)
        btns.grid(row=5, column=0, columnspan=4, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="OK", command=self.ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left")

        self.bind("<Return>", lambda e: self.ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.wait_window()

    def pick_src_dir(self):
        p = filedialog.askdirectory(parent=self, title="Folder containing the game's saves",
                                    initialdir=self.src.get() or None)
        if p:
            self.src.set(os.path.normpath(p))

    def pick_src_file(self):
        p = filedialog.askopenfilename(parent=self, title="Save file")
        if p:
            self.src.set(os.path.normpath(p))

    def pick_dest(self):
        p = filedialog.askdirectory(parent=self, title="Folder to store backups in",
                                    initialdir=self.dest.get() or None)
        if p:
            self.dest.set(os.path.normpath(p))

    def ok(self):
        name, src, dest = self.name.get().strip(), self.src.get().strip(), self.dest.get().strip()
        try:
            interval, keep = int(self.interval.get()), int(self.keep.get())
            if interval < 0 or keep < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "Interval and keep count must be whole numbers, 0 or more.", parent=self)
            return
        if not name or not src or not dest:
            messagebox.showerror(APP_NAME, "Name, save location and backup folder are all required.", parent=self)
            return
        if not os.path.exists(src):
            messagebox.showerror(APP_NAME, "The save location does not exist.", parent=self)
            return
        src_abs, dest_abs = os.path.abspath(src).lower(), os.path.abspath(dest).lower()
        if os.path.isdir(src) and (dest_abs + os.sep).startswith(src_abs + os.sep):
            messagebox.showerror(APP_NAME, "The backup folder cannot be inside the save folder.", parent=self)
            return
        self.result = {"name": name, "src": src, "dest": dest, "interval": interval, "keep": keep}
        self.destroy()


class RestoreDialog(tk.Toplevel):
    def __init__(self, app, game):
        super().__init__(app)
        self.app, self.game = app, game
        self.title(f"Restore - {game['name']}")
        self.transient(app)
        self.geometry("760x380")

        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Choose a backup to restore. Close the game (or return to its main menu) first.\n"
                          "Your current save is backed up automatically before it is replaced.\n"
                          "Select several backups (Ctrl/Shift+click) to delete them together; "
                          "only one can be restored at a time.").pack(anchor="w")
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True, pady=8)
        self.tree = ttk.Treeview(box, columns=("file", "when", "kind", "size"), show="headings",
                                 selectmode="extended")
        for col, text, width in (("file", "File name", 260), ("when", "Date / time", 150),
                                 ("kind", "Type", 120), ("size", "Size", 90)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        sb = ttk.Scrollbar(box, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.restore())

        btns = ttk.Frame(f)
        btns.pack(fill="x")
        ttk.Button(btns, text="Restore selected", command=self.restore).pack(side="left")
        ttk.Button(btns, text="Delete selected", command=self.delete).pack(side="left", padx=6)
        ttk.Button(btns, text="Open folder", command=lambda: app.open_folder(game)).pack(side="left")
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for path, when, kind, size in list_backups(self.game):
            self.tree.insert("", "end", iid=path,
                             values=(os.path.basename(path), when, KIND_LABELS[kind], fmt_size(size)))

    def selected(self):
        """All selected backup paths, in list order."""
        return list(self.tree.selection())

    def restore(self):
        sel = self.selected()
        if not sel:
            return
        if len(sel) > 1:
            messagebox.showinfo(APP_NAME, "Select a single backup to restore.", parent=self)
            return
        path = sel[0]
        when = self.tree.item(path, "values")[1]
        if messagebox.askyesno(APP_NAME, f"Replace the current {self.game['name']} save with the backup "
                                         f"from {when}?", parent=self):
            self.app.jobs.put({"op": "restore", "game": dict(self.game), "zip": path})
            self.destroy()

    def delete(self):
        sel = self.selected()
        if not sel:
            return
        if len(sel) == 1:
            question = "Delete this backup permanently?"
        else:
            question = f"Delete these {len(sel)} backups permanently?"
        if messagebox.askyesno(APP_NAME, question, parent=self):
            failed = []
            for path in sel:
                try:
                    os.remove(path)
                except OSError as e:
                    failed.append(f"{os.path.basename(path)}: {e.strerror or e}")
            self.refresh()
            if failed:
                messagebox.showerror(APP_NAME, "Could not delete:\n" + "\n".join(failed), parent=self)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("860x480")
        self.minsize(700, 380)

        self.config_data = self.load_config()
        self.games = self.config_data["games"]
        self.ui_queue = queue.Queue()
        self.jobs = queue.Queue()
        # First automatic check shortly after launch, then every interval.
        self.next_due = {g["id"]: time.time() + 10 for g in self.games}

        self.build_ui()
        self.refresh_tree()

        threading.Thread(target=self.worker_loop, daemon=True).start()
        self.hotkey = HotkeyThread(lambda: self.ui_queue.put(("hotkey",)),
                                   lambda ok: self.ui_queue.put(("hotkey_status", ok)))
        self.hotkey.start()
        self.apply_hotkey(save=False)

        self.after(100, self.poll_queue)
        self.after(1000, self.scheduler_tick)
        self.log("Started. Keep this window open (minimized is fine) for automatic backups and the hotkey.")

    # -- config

    def load_config(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data.setdefault("games", [])
        data.setdefault("hotkey", DEFAULT_HOTKEY)
        return data

    def save_config(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.config_data, f, indent=2)
        os.replace(tmp, CONFIG_PATH)

    # -- layout

    def build_ui(self):
        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.pack(fill="both", expand=True)
        cols = (("active", "Active", 50), ("name", "Game", 150), ("src", "Save location", 300), ("interval", "Every", 70),
                ("last", "Last backup", 140))
        self.tree = ttk.Treeview(top, columns=[c[0] for c in cols], show="headings", selectmode="browse", height=8)
        for col, text, width in cols:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="center" if col == "active" else "w")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.edit_game())

        side = ttk.Frame(top)
        side.pack(side="left", fill="y", padx=(8, 0))
        for text, cmd in (("Add game…", self.add_game), ("Edit…", self.edit_game), ("Remove", self.remove_game),
                          ("Set as active", self.set_active),
                          (None, None),
                          ("Back up now", self.backup_active), ("Restore…", self.restore_game),
                          ("Open backups", self.open_active_folder),
                          (None, None),
                          ("Back up all", self.backup_all)):
            if text is None:
                ttk.Separator(side).pack(fill="x", pady=8)
                if not hasattr(self, "active_label"):
                    ttk.Label(side, text="Active game:").pack(anchor="w")
                    self.active_label = ttk.Label(side, text="", font=("Segoe UI", 9, "bold"), wraplength=110)
                    self.active_label.pack(anchor="w", pady=(0, 4))
            else:
                ttk.Button(side, text=text, command=cmd, width=14).pack(pady=2)

        opts = ttk.Frame(self, padding=(10, 4))
        opts.pack(fill="x")
        ttk.Label(opts, text="Backup hotkey (backs up the active game):").pack(side="left")
        self.hotkey_var = tk.StringVar(value=self.config_data["hotkey"])
        ttk.Entry(opts, textvariable=self.hotkey_var, width=18).pack(side="left", padx=6)
        ttk.Button(opts, text="Apply", command=self.apply_hotkey).pack(side="left")
        self.hotkey_state = ttk.Label(opts, text="")
        self.hotkey_state.pack(side="left", padx=8)
        self.startup_var = tk.BooleanVar(value=startup_enabled())
        ttk.Checkbutton(opts, text="Start with Windows", variable=self.startup_var,
                        command=self.toggle_startup).pack(side="right")

        logf = ttk.Frame(self, padding=(10, 4, 10, 10))
        logf.pack(fill="both")
        self.log_text = tk.Text(logf, height=7, state="disabled", wrap="word", font=("Segoe UI", 9))
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

    def refresh_tree(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        active = self.active_game(quiet=True)
        self.active_label.configure(text=active["name"] if active else "(none)")
        for g in self.games:
            every = f"{g['interval']} min" if g["interval"] else "manual"
            self.tree.insert("", "end", iid=g["id"],
                             values=("●" if g is active else "", g["name"], g["src"], every,
                                     g.get("last_backup", "never")))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])

    def log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # -- game list actions

    def selected_game(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select a game in the list first.", parent=self)
            return None
        return next(g for g in self.games if g["id"] == sel[0])

    def active_game(self, quiet=False):
        """The game the hotkey and backup/restore buttons act on; falls back
        to the first game when none has been chosen."""
        for g in self.games:
            if g["id"] == self.config_data.get("active"):
                return g
        if self.games:
            return self.games[0]
        if not quiet:
            messagebox.showinfo(APP_NAME, "Add a game first.", parent=self)
        return None

    def set_active(self):
        g = self.selected_game()
        if g:
            self.config_data["active"] = g["id"]
            self.save_config()
            self.refresh_tree()
            self.log(f"Active game: {g['name']}")

    def add_game(self):
        default_dest = self.games[-1]["dest"] if self.games else ""
        dlg = GameDialog(self, default_dest=default_dest)
        if dlg.result:
            g = dict(dlg.result, id=uuid.uuid4().hex)
            self.games.append(g)
            self.next_due[g["id"]] = time.time() + 10
            self.save_config()
            self.refresh_tree()

    def edit_game(self):
        g = self.selected_game() if self.tree.selection() else None
        if not g:
            return
        dlg = GameDialog(self, game=g)
        if dlg.result:
            old_dir = backup_dir(g)
            g.update(dlg.result)
            new_dir = backup_dir(g)
            # Carry existing backups along when the game is renamed.
            if old_dir != new_dir and os.path.isdir(old_dir) and not os.path.exists(new_dir):
                try:
                    os.makedirs(os.path.dirname(new_dir), exist_ok=True)
                    shutil.move(old_dir, new_dir)
                except OSError as e:
                    self.log(f"Could not move existing backups to {new_dir}: {e}")
            self.next_due[g["id"]] = time.time() + 10
            self.save_config()
            self.refresh_tree()

    def remove_game(self):
        g = self.selected_game()
        if g and messagebox.askyesno(APP_NAME, f"Remove {g['name']} from the list?\n\n"
                                               "Existing backup files are not deleted.", parent=self):
            self.games.remove(g)
            self.save_config()
            self.refresh_tree()

    def queue_backup(self, game, kind, beep=False):
        self.jobs.put({"op": "backup", "game": dict(game), "kind": kind, "beep": beep})

    def backup_active(self, beep=False):
        g = self.active_game(quiet=beep)
        if g:
            self.queue_backup(g, "manual", beep)
        elif beep:
            self.log("No games configured yet.")

    def backup_all(self):
        if not self.games:
            self.log("No games configured yet.")
        for g in self.games:
            self.queue_backup(g, "manual")

    def restore_game(self):
        g = self.active_game()
        if g:
            RestoreDialog(self, g)

    def open_folder(self, game):
        d = backup_dir(game)
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    def open_active_folder(self):
        g = self.active_game()
        if g:
            self.open_folder(g)

    # -- options

    def apply_hotkey(self, save=True):
        text = self.hotkey_var.get().strip()
        if not text:
            self.hotkey.set(None)
            self.hotkey_state.configure(text="disabled")
        else:
            try:
                self.hotkey.set(parse_hotkey(text))
            except ValueError as e:
                messagebox.showerror(APP_NAME, f"Invalid hotkey: {e}", parent=self)
                return
        if save:
            self.config_data["hotkey"] = text
            self.save_config()

    def toggle_startup(self):
        try:
            set_startup(self.startup_var.get())
        except OSError as e:
            self.startup_var.set(startup_enabled())
            messagebox.showerror(APP_NAME, f"Could not change the startup setting: {e}", parent=self)

    # -- background work

    def worker_loop(self):
        while True:
            job = self.jobs.get()
            g = job["game"]
            ok = True
            try:
                if job["op"] == "backup":
                    res = do_backup(g, job["kind"])
                    if res:
                        path, sig = res
                        self.ui_queue.put(("sig", g["id"], sig, time.strftime("%Y-%m-%d %H:%M")))
                        self.ui_queue.put(("log", f"{g['name']}: {KIND_LABELS[job['kind']].lower()} backup saved "
                                                  f"({os.path.basename(path)}, {fmt_size(os.path.getsize(path))})"))
                else:
                    sig = do_restore(g, job["zip"])
                    self.ui_queue.put(("sig", g["id"], sig, None))
                    self.ui_queue.put(("log", f"{g['name']}: restored {os.path.basename(job['zip'])}"))
            except Exception as e:
                ok = False
                what = "restore" if job["op"] == "restore" else "backup"
                self.ui_queue.put(("log", f"{g['name']}: {what} FAILED - {e}"))
            if job.get("beep"):
                winsound.Beep(1000 if ok else 300, 120 if ok else 500)

    def poll_queue(self):
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                if msg[0] == "log":
                    self.log(msg[1])
                elif msg[0] == "sig":
                    _, gid, sig, when = msg
                    for g in self.games:
                        if g["id"] == gid:
                            g["last_sig"] = sig
                            if when:
                                g["last_backup"] = when
                    self.save_config()
                    self.refresh_tree()
                elif msg[0] == "hotkey":
                    self.log("Hotkey pressed - backing up the active game.")
                    self.backup_active(beep=True)
                elif msg[0] == "hotkey_status":
                    self.hotkey_state.configure(
                        text="active" if msg[1] else "could not register - in use by another program")
        except queue.Empty:
            pass
        self.after(100, self.poll_queue)

    def scheduler_tick(self):
        now = time.time()
        for g in self.games:
            if g["interval"] > 0 and now >= self.next_due.get(g["id"], 0):
                self.next_due[g["id"]] = now + g["interval"] * 60
                self.queue_backup(g, "auto")
        self.after(5000, self.scheduler_tick)


if __name__ == "__main__":
    app = App()
    if "--minimized" in sys.argv:
        app.iconify()
    app.mainloop()
