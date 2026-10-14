# Palworld Host Save Fixer - created by vanloads
# Licensed under GPL-3.0 (see LICENSE)
"""Step-by-step guide tab.

Looks at what is on this PC (worlds, a local dedicated server, whether the
game or server is running, whether a map got covered) and walks the user
through the move one simple step at a time.  The actual character transfer is
still done by the Transfer tab; the guide only loads the right folder into it.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .gvas import parse_gvas
from .savfile import read_sav
from .world import HOST_UID, TransferError

SERVER_ADDRESS = "127.0.0.1:8211"

PRIVACY = """\
What this app looks at:
  • Only the folders YOU pick with a "Pick..." button. It never searches your PC.
  • If you say OK below: which programs are open, so it can tell you when
    Palworld or the server still needs to be closed.

It changes files only when you click a button that says so, and it makes a
backup first. It never connects to the internet and never sends anything
anywhere. The full source code is public on GitHub."""

# Set by the "Before we start" screen. Until the user says OK, the guide
# doesn't read the list of running programs.
ALLOWED = False
OK, BAD, INFO = "ok", "bad", "info"
COLORS = {OK: "#1a7f37", BAD: "#c62828", INFO: "#555555"}
ICONS = {OK: "\u2714", BAD: "\u2716", INFO: "\u2192"}


# -- what's on this PC ---------------------------------------------------
def running_programs() -> set[str] | None:
    """Lower-case names of running programs, or None if we couldn't (or may not) tell."""
    if not ALLOWED:
        return None
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                             errors="ignore", timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {line.split(",")[0].strip('"').lower() for line in out.splitlines() if line.strip()}


def game_running(progs: set[str] | None) -> bool | None:
    return None if progs is None else any(p.startswith("palworld") for p in progs)


def server_running(progs: set[str] | None) -> bool | None:
    return None if progs is None else any(p.startswith("palserver") for p in progs)


def world_name(folder: Path) -> str:
    try:
        data, _ = read_sav(folder / "LevelMeta.sav")
        _, r = parse_gvas(data)
        wn = r.path("SaveData", "WorldName")
        if wn is not None and wn.value:
            return str(wn.value)
    except Exception:
        pass
    return ""


@dataclass
class LocalWorld:
    folder: Path
    name: str
    hosted: bool        # has Level.sav: a world this PC hosted in co-op
    modified: float

    @property
    def world_id(self) -> str:
        return self.folder.name

    def label(self) -> str:
        nm = self.name or "World"
        when = time.strftime("%b %d", time.localtime(self.modified))
        kind = "you hosted" if self.hosted else "you joined"
        warn = "   \u26a0 map looks covered" if map_looks_covered(self.folder) else ""
        return f"{nm}  ({kind}, last played {when}, ID {self.world_id[:8]}...){warn}"


def save_games_root() -> Path | None:
    la = os.environ.get("LOCALAPPDATA")
    return Path(la) / "Pal" / "Saved" / "SaveGames" if la else None


def open_world(folder: Path) -> LocalWorld | None:
    """A world folder the user picked, or None if it isn't one."""
    hosted = (folder / "Level.sav").is_file()
    if not (hosted or (folder / "LocalData.sav").is_file()):
        return None
    mod = max((p.stat().st_mtime for p in (folder / "Level.sav", folder / "LocalData.sav") if p.is_file()),
              default=0)
    return LocalWorld(folder, world_name(folder) if hosted else "", hosted, mod)


@dataclass
class Server:
    root: Path              # folder holding PalServer.exe

    @property
    def exe(self) -> Path:
        return self.root / "PalServer.exe"

    @property
    def saves(self) -> Path:
        return self.root / "Pal" / "Saved" / "SaveGames" / "0"

    @property
    def ini(self) -> Path:
        return self.root / "Pal" / "Saved" / "Config" / "WindowsServer" / "GameUserSettings.ini"

    def world_id(self) -> str:
        try:
            m = re.search(r"^\s*DedicatedServerName\s*=\s*([0-9A-Fa-f]*)",
                          self.ini.read_text(encoding="utf-8", errors="ignore"),
                          re.M)
        except OSError:
            return ""
        return m.group(1).upper() if m else ""


# -- map exploration (LocalData.sav) -------------------------------------
@dataclass
class MapCopy:
    path: Path
    when: datetime
    size: int
    note: str = ""


def map_copies(world: Path) -> list[MapCopy]:
    """Every copy of the map we can offer: the current one, the game's own
    backups and copies set aside by this tool. Newest first."""
    out = []
    cur = world / "LocalData.sav"
    if cur.is_file():
        st = cur.stat()
        out.append(MapCopy(cur, datetime.fromtimestamp(st.st_mtime), st.st_size, "the map you have now"))
    for p in (world / "backup" / "local").glob("*/LocalData.sav"):
        try:
            when = datetime.strptime(p.parent.name, "%Y.%m.%d-%H.%M.%S")
        except ValueError:
            when = datetime.fromtimestamp(p.stat().st_mtime)
        out.append(MapCopy(p, when, p.stat().st_size, "the game's automatic backup"))
    for p in world.glob("LocalData.sav.bak_*"):
        try:
            when = datetime.strptime(p.name.split("bak_", 1)[1], "%Y%m%d-%H%M%S")
        except ValueError:
            when = datetime.fromtimestamp(p.stat().st_mtime)
        out.append(MapCopy(p, when, p.stat().st_size, "saved before an earlier fix"))
    out.sort(key=lambda c: c.when, reverse=True)
    return out


def map_looks_covered(world: Path) -> bool:
    """A map file grows as you explore. If the current one is far smaller than
    an older copy, the game most likely saved a fresh map over it."""
    copies = map_copies(world)
    cur = next((c for c in copies if c.path.name == "LocalData.sav" and c.path.parent == world), None)
    biggest = max((c.size for c in copies), default=0)
    return cur is not None and biggest > 0 and cur.size < biggest * 0.5


def best_map(copies: list[MapCopy]) -> MapCopy | None:
    """The newest copy that's about as explored as the biggest one. Sizes wobble
    a little between saves, so "about" is within 10%."""
    biggest = max((c.size for c in copies), default=0)
    return max((c for c in copies if c.size >= biggest * 0.9), key=lambda c: c.when, default=None)


def restore_map(world: Path, src: Path, log=print) -> Path | None:
    if game_running(running_programs()):
        raise TransferError("Palworld is still open. Close the game completely, then try again.")
    dst = world / "LocalData.sav"
    if src.resolve() == dst.resolve():
        raise TransferError("That's the map you already have. Pick an older one.")
    bak = None
    if dst.exists():
        bak = dst.with_name(f"LocalData.sav.bak_{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(dst, bak)
        log(f"Your current map was saved as {bak.name} (in case you want it back).")
    shutil.copy2(src, dst)
    log(f"Map restored from {src}")
    return bak


# -- files to send -------------------------------------------------------
def desktop() -> Path:
    for p in (Path(os.environ.get("OneDrive", "")) / "Desktop", Path.home() / "Desktop"):
        if p.is_dir() and str(p) != "Desktop":
            return p
    return Path.home()


def zip_folder(folder: Path, dest: Path, only: set[str] | None = None, skip: set[str] = frozenset()) -> Path:
    """Zip ``folder`` (kept as the top folder inside the zip). ``only`` limits it
    to those top-level names; ``skip`` leaves those out."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in folder.rglob("*"):
            top = p.relative_to(folder).parts[0]
            if not p.is_file() or top in skip or (only is not None and top not in only):
                continue
            z.write(p, Path(folder.name) / p.relative_to(folder))
    return dest


def copy_world_to_server(world: LocalWorld, srv: Server, log=print) -> None:
    if server_running(running_programs()):
        raise TransferError("The server is running. Close the server window first, then try again.")
    target = srv.saves / world.world_id
    if (target / "Level.sav").exists():
        raise TransferError("This world is already on the server. Nothing was changed, so the "
                            "server's progress is safe.")
    log(f"Copying {world.folder} to {target} ...")
    shutil.copytree(world.folder, target, ignore=shutil.ignore_patterns("backup"), dirs_exist_ok=True)
    ini = srv.ini
    ini.parent.mkdir(parents=True, exist_ok=True)
    text = ini.read_text(encoding="utf-8", errors="ignore") if ini.exists() else ""
    if ini.exists():
        shutil.copy2(ini, ini.with_name(ini.name + f".bak_{time.strftime('%Y%m%d-%H%M%S')}"))
    line = f"DedicatedServerName={world.world_id}"
    if re.search(r"^\s*DedicatedServerName\s*=.*$", text, re.M):
        text = re.sub(r"^\s*DedicatedServerName\s*=.*$", line, text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + ("\n\n" if text else "") + "[/Script/Pal.PalGameLocalSettings]\n" + line + "\n"
    ini.write_text(text, encoding="utf-8")
    log(f"Server is now set to use world {world.world_id}.")


def host_left_behind(world_folder: Path | None) -> bool | None:
    """True while the old co-op host character is still waiting to be moved."""
    if world_folder is None or not (world_folder / "Level.sav").is_file():
        return None
    return (world_folder / "Players" / f"{HOST_UID}.sav").is_file()


def new_characters(world_folder: Path) -> list[Path]:
    players = world_folder / "Players"
    return [p for p in players.glob("*.sav")
            if not p.stem.endswith("_dps") and p.stem.upper() != HOST_UID] if players.is_dir() else []


# -- the tab -------------------------------------------------------------
@dataclass
class Step:
    title: str
    text: str
    check: object = None                         # () -> (state, message), runs off the UI thread
    actions: list = field(default_factory=list)  # [(button text, callback)]
    extra: object = None                         # (parent frame) -> None, builds extra widgets


class GuideTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=14)
        self.app = app
        self.worlds: list[LocalWorld] = []       # only folders the user picked
        self.world: LocalWorld | None = None     # the co-op world being moved
        self.server: Server | None = None        # server on this PC
        self.returned: Path | None = None        # world folder sent back by the friend
        self.progs: set[str] | None = set()
        self.declined = False
        self.history: list = []
        self.flow: list[Step] = []
        self.idx = 0
        self._checking = False

        st = ttk.Style(self)
        st.configure("GTitle.TLabel", font=("Segoe UI", 17, "bold"))
        st.configure("GText.TLabel", font=("Segoe UI", 12))
        st.configure("GSmall.TLabel", font=("Segoe UI", 10), foreground="#555")
        st.configure("GBig.TButton", font=("Segoe UI", 12), padding=(14, 10))
        st.configure("G.TButton", font=("Segoe UI", 11), padding=(10, 6))

        top = ttk.Frame(self)
        top.pack(fill="x")
        self.game_lbl = tk.Label(top, font=("Segoe UI", 10, "bold"))
        self.game_lbl.pack(side="left")
        self.srv_lbl = tk.Label(top, font=("Segoe UI", 10, "bold"))
        self.srv_lbl.pack(side="left", padx=18)
        ttk.Button(top, text="Start over", style="G.TButton", command=self.home).pack(side="right")
        self.back_btn = ttk.Button(top, text="\u25c0 Back", style="G.TButton", command=self.back)
        self.back_btn.pack(side="right", padx=6)
        ttk.Separator(self).pack(fill="x", pady=8)

        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)

        self.consent_page()
        self.after(500, self._tick)

    # -- background ------------------------------------------------------
    def _tick(self):
        """Every few seconds: is the game/server open, and re-run the current check."""
        if not self._checking:
            self._checking = True
            step = self.flow[self.idx] if self.flow and self.idx < len(self.flow) else None

            def work():
                progs = running_programs()
                res = None
                if step is not None and step.check:
                    try:
                        res = step.check()
                    except Exception as e:
                        res = (INFO, f"Couldn't check this: {e}")

                def show():
                    self._checking = False
                    self.progs = progs
                    self._show_running()
                    if res and step is self._cur_step():
                        self._show_check(res)
                self.app.ui(show)
            threading.Thread(target=work, daemon=True).start()
        self.after(3000, self._tick)

    def _show_running(self):
        if not ALLOWED:
            self.game_lbl.configure(text="Not checking open programs (you said no thanks)"
                                    if self.declined else "", fg="#555")
            self.srv_lbl.configure(text="")
            return
        g, s = game_running(self.progs), server_running(self.progs)
        self.game_lbl.configure(
            text={True: "\u25cf Palworld game: OPEN", False: "\u25cf Palworld game: closed"}.get(g, "Palworld game: ?"),
            fg="#c62828" if g else "#1a7f37")
        self.srv_lbl.configure(text="\u25cf Server on this PC: RUNNING" if s else "\u25cf Server on this PC: not running",
                               fg="#1a7f37" if s else "#555")

    # -- page plumbing -------------------------------------------------------
    def _clear(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.check_lbl = None

    def _go(self, page, *args):
        self.history.append((page, args))
        self.flow = []
        page(*args)

    def back(self):
        if self.flow and self.idx > 0:
            self.idx -= 1
            self._render_step()
            return
        if len(self.history) > 1:
            self.history.pop()
            page, args = self.history.pop()
            self._go(page, *args)

    def _title(self, text, sub=None):
        ttk.Label(self.body, text=text, style="GTitle.TLabel", wraplength=820, justify="left").pack(anchor="w")
        if sub:
            ttk.Label(self.body, text=sub, style="GText.TLabel", wraplength=820,
                      justify="left").pack(anchor="w", pady=(6, 12))

    def _choice(self, text, hint, cmd):
        b = ttk.Button(self.body, text=text, style="GBig.TButton", command=cmd)
        b.pack(fill="x", pady=(8, 0))
        ttk.Label(self.body, text=hint, style="GSmall.TLabel", wraplength=820).pack(anchor="w", padx=6)

    def _notice(self, text, color, button=None):
        f = tk.Frame(self.body, bg="#fff4e5" if color == BAD else "#e8f5e9", padx=10, pady=8)
        f.pack(fill="x", pady=(0, 10))
        tk.Label(f, text=text, bg=f["bg"], font=("Segoe UI", 11), wraplength=640,
                 justify="left").pack(side="left")
        if button:
            ttk.Button(f, text=button[0], command=button[1]).pack(side="right")

    # -- pages -----------------------------------------------------------
    def consent_page(self):
        self.history, self.flow = [], []
        self._clear()
        self._title("Before we start",
                    "You'll pick your folders yourself, so nothing on your PC gets searched. One question:")
        tk.Label(self.body, text=PRIVACY, font=("Segoe UI", 11), justify="left", anchor="w", bg="#eef3fb",
                 padx=12, pady=10).pack(anchor="w", fill="x", pady=(0, 6))
        self._choice("OK, check if Palworld is open", "Recommended. The app warns you before a change that "
                                                      "an open game or server would undo.", self.allow)
        self._choice("No thanks",
                     "Everything still works. The app will ask you to make sure things are closed instead.",
                     self.decline)

    def allow(self):
        global ALLOWED
        ALLOWED = True
        self.declined = False
        self.home()

    def decline(self):
        global ALLOWED
        ALLOWED = False
        self.declined = True
        self.app.log("OK. The app won't check which programs are open.")
        self.home()

    def show_privacy(self):
        messagebox.showinfo("What this app looks at", PRIVACY)

    def home(self):
        if not ALLOWED and not self.declined:
            return self.consent_page()
        self.history, self.flow = [(self.home, ())], []
        self._clear()
        self._title("Hi! What happened?", "Pick the one that sounds like you. The app will walk you through it "
                                           "one step at a time.")
        covered = [w for w in self.worlds if map_looks_covered(w.folder)]
        if covered:
            self._notice(f"\u26a0 Heads up: your map in \"{covered[0].name or covered[0].world_id[:8]}\" looks "
                         "covered again. That usually happens after joining a server. It can be fixed!",
                         BAD, ("Fix my map", lambda: self._go(self.map_page, covered[0])))
        self._choice("I was the HOST of a co-op world, and now it's on a dedicated server",
                     "The server made you a brand-new character and your old one is missing.",
                     lambda: self._go(self.where_page))
        self._choice("My map is covered again (the fog came back)",
                     "Your explored map disappeared after moving to a server.",
                     lambda: self._go(self.map_page, None))
        self._choice("I JOINED someone else's co-op world (I wasn't the host)",
                     "You probably don't need this app. Tap to see why.",
                     lambda: self._go(self.joiner_page))
        row = ttk.Frame(self.body)
        row.pack(anchor="w", pady=(18, 0))
        ttk.Button(row, text="What does this app look at?", command=self.show_privacy).pack(side="left")
        if not ALLOWED:
            ttk.Button(row, text="Let the app check if Palworld is open", command=self.allow).pack(side="left", padx=8)

    def joiner_page(self):
        self._clear()
        self._title("Good news: you're all set!",
                    "Only the person who HOSTED the co-op world loses their character when it moves to a "
                    "server. If you joined someone else's world, your character comes with you on its own.\n\n"
                    "Your map is saved on your own PC too, so it should already be there when you join the "
                    "server.\n\nIf your map IS covered anyway, use the button below.")
        ttk.Button(self.body, text="My map is covered", style="GBig.TButton",
                   command=lambda: self._go(self.map_page, None)).pack(anchor="w", pady=10)

    def where_page(self):
        self._clear()
        self._title("Where is the server running?")
        self._choice("On THIS computer", "You run the server yourself, on this PC.",
                     lambda: self._go(self._start_flow, self._local_server_steps()))
        self._choice("On a FRIEND'S computer (or a server company)",
                     "Someone else runs the server. You'll swap files with them.",
                     lambda: self._go(self._start_flow, self._friend_server_steps()))

    def _start_flow(self, steps):
        self.flow, self.idx = steps, 0
        self._render_step()

    def _cur_step(self):
        return self.flow[self.idx] if self.flow and self.idx < len(self.flow) else None

    def _render_step(self):
        self._clear()
        step = self.flow[self.idx]
        ttk.Label(self.body, text=f"STEP {self.idx + 1} OF {len(self.flow)}", style="GSmall.TLabel").pack(anchor="w")
        self._title(step.title, step.text)
        if step.extra:
            step.extra(self.body)
        if step.actions:
            row = ttk.Frame(self.body)
            row.pack(anchor="w", pady=(4, 8))
            for text, cb in step.actions:
                ttk.Button(row, text=text, style="G.TButton", command=cb).pack(side="left", padx=(0, 8))
        self.check_lbl = tk.Label(self.body, font=("Segoe UI", 12, "bold"), wraplength=820, justify="left",
                                  text="Checking..." if step.check else "", fg="#555")
        self.check_lbl.pack(anchor="w", pady=8)
        nav = ttk.Frame(self.body)
        nav.pack(side="bottom", fill="x")
        if self.idx < len(self.flow) - 1:
            ttk.Button(nav, text="Next step \u25b6", style="GBig.TButton", command=self._next).pack(side="right")
        else:
            ttk.Button(nav, text="All done!", style="GBig.TButton", command=self.home).pack(side="right")
        self.recheck()

    def _next(self):
        self.idx += 1
        self._render_step()

    def recheck(self):
        step = self._cur_step()
        if step is None or not step.check:
            return

        def work():
            try:
                res = step.check()
            except Exception as e:
                res = (INFO, f"Couldn't check this: {e}")
            self.app.ui(lambda: step is self._cur_step() and self._show_check(res))
        threading.Thread(target=work, daemon=True).start()

    def _show_check(self, res):
        if self.check_lbl is not None and self.check_lbl.winfo_exists():
            state, msg = res
            self.check_lbl.configure(text=f"{ICONS[state]}  {msg}", fg=COLORS[state])

    # -- shared widgets & actions -----------------------------------------------
    def _world_picker(self, parent):
        if self.world is None:
            ttk.Label(parent, text="Pick your co-op world folder. It's usually in\n"
                                   "%LOCALAPPDATA%\\Pal\\Saved\\SaveGames\\<your Steam ID>\\<a long World ID>",
                      style="GText.TLabel").pack(anchor="w")
            ttk.Button(parent, text="Pick my world folder...", style="G.TButton",
                       command=self._browse_world).pack(anchor="w", pady=(2, 8))
            return
        ttk.Label(parent, text=f"Your world:  {self.world.label()}", style="GText.TLabel").pack(anchor="w")
        ttk.Button(parent, text="Pick a different folder...",
                   command=self._browse_world).pack(anchor="w", pady=(2, 8))

    def _browse_world(self, then=None):
        """Let the user point at a world folder. The dialog opens where saves
        usually are, but nothing is read until they pick."""
        root = save_games_root()
        d = filedialog.askdirectory(title="Pick your world folder (the one named with a long ID)",
                                    initialdir=str(root) if root and root.is_dir() else None)
        if not d:
            return
        p = Path(d)
        w = next((k for k in self.worlds if k.folder == p), None) or open_world(p)
        if w is None:
            messagebox.showinfo("Guide", "That folder doesn't look like a Palworld world. Pick the folder "
                                         "named with a long ID, like 96D6CBDB4056...")
            return
        if w not in self.worlds:
            self.worlds.insert(0, w)
        self.world = w
        (then or self._render_step)()

    def _confirm_closed(self, what="Palworld") -> bool:
        """True if it's safe to go on. Asks the user when we aren't allowed to check."""
        progs = running_programs()
        open_now = game_running(progs) if what == "Palworld" else server_running(progs)
        if open_now:
            messagebox.showwarning("Guide", f"{what} is still open.\n\nClose it completely, then click again.")
            return False
        if open_now is None:
            return messagebox.askyesno("Guide", f"Is {what} completely closed?\n\nIf it's open, it will "
                                                "undo this change.")
        return True

    def _open(self, path: Path | None):
        if path is None:
            messagebox.showinfo("Guide", "Pick a world first.")
            return
        if not path.exists():
            messagebox.showinfo("Guide", f"That doesn't exist yet:\n\n{path}")
            return
        os.startfile(path)

    def _copy(self, text, what):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.log(f"Copied {what} to the clipboard: {text}")
        messagebox.showinfo("Guide", f"Copied! Paste it with Ctrl+V.\n\n{text}")

    def _start_server(self):
        if self.server is None:
            messagebox.showinfo("Guide", "I don't know where your server is yet. Go back to step 1.")
            return
        if server_running(running_programs()):
            messagebox.showinfo("Guide", "The server is already running.")
            return
        subprocess.Popen([str(self.server.exe)], cwd=str(self.server.root))
        self.app.log(f"Started the server: {self.server.exe}")

    def _to_transfer(self, folder: Path | None):
        if folder is None or not (folder / "Level.sav").is_file():
            messagebox.showinfo("Guide", "I can't find that world yet. Go back a step.")
            return
        if game_running(running_programs()) or (folder == self._server_world() and server_running(running_programs())):
            if not messagebox.askyesno("Guide", "The game or server still looks open. Changes made while "
                                                "they run get overwritten.\n\nOpen the Transfer tab anyway?"):
                return
        self.app.folder_var.set(str(folder))
        self.app._load()
        self.app.nb.select(self.app.tab_main)
        messagebox.showinfo("Guide", "The Transfer tab is ready.\n\n"
                                     "Keep = your OLD character (it says CO-OP HOST).\n"
                                     "Replace = the NEW character the server made.\n\n"
                                     "Click 'Preview changes', then 'Transfer character'. "
                                     "Then come back to the Guide tab for the next step.")

    def _server_world(self) -> Path | None:
        if self.server is None or self.world is None:
            return None
        return self.server.saves / self.world.world_id

    def _make_zip(self, folder, name, **kw):
        dest = desktop() / name

        def job():
            return zip_folder(folder, dest, **kw)

        def done(p):
            self.app.log(f"Made {p}")
            messagebox.showinfo("Guide", f"Done! It's on your Desktop:\n\n{p.name}")
            os.startfile(p.parent)
        self.app.run_bg(job, done)

    # -- server on this PC ---------------------------------------------------
    def _local_server_steps(self) -> list[Step]:
        def browse_server():
            d = filedialog.askdirectory(title="Pick the folder that has PalServer.exe in it")
            if d and (Path(d) / "PalServer.exe").is_file():
                self.server = Server(Path(d))
                self.recheck()
            elif d:
                messagebox.showinfo("Guide", "That folder doesn't have PalServer.exe in it. Pick the "
                                             "folder that does.")

        def check_server():
            if self.server is None:
                return BAD, ("Click 'Pick the server folder'. No server yet? Install 'Palworld Dedicated "
                             "Server' from your Steam library (under Tools).")
            return OK, f"Your server: {self.server.root}"

        def check_world_on_server():
            if self.server is None:
                return BAD, "Go back a step and pick your server folder first."
            if self.world is None:
                return BAD, "Click 'Pick my world folder' and choose your co-op world."
            sw = self._server_world()
            there = (sw / "Level.sav").is_file()
            using = self.server.world_id() == self.world.world_id
            if there and using:
                return OK, "Your world is on the server, and the server is set to use it."
            if there:
                return BAD, ("Your world is on the server, but the server is set to a different world. "
                             "Click 'Do it for me' or fix DedicatedServerName in the settings file.")
            return BAD, "Your world isn't on the server yet. Click 'Do it for me'."

        def do_copy():
            if self.server is None or self.world is None:
                return
            w, s = self.world, self.server
            if not self._confirm_closed("the server"):
                return
            if not messagebox.askyesno("Guide", f"Copy \"{w.name or w.world_id}\" to the server and set "
                                                "the server to use it?"):
                return
            self.app.run_bg(lambda: copy_world_to_server(w, s, self.app.log), lambda _: self.recheck())

        def check_new_char():
            sw = self._server_world()
            if sw is None or not sw.is_dir():
                return BAD, "Your world isn't on the server yet. Go back a step."
            if not host_left_behind(sw):
                return OK, "Your old character has already been moved. You can skip ahead."
            n = len(new_characters(sw))
            if n:
                return OK, (f"The server has saved {n} new character(s). One should be your throw-away. "
                            "Quit to the title screen before going on.")
            return BAD, ("The server hasn't saved a new character yet. Join, make the character, "
                         "play about a minute, then quit to the title screen.")

        def check_closed():
            progs = running_programs()
            g, s = game_running(progs), server_running(progs)
            if progs is None:
                return INFO, "Make sure the Palworld game and the server window are both closed."
            if g and s:
                return BAD, "Close the Palworld game AND the server window."
            if g:
                return BAD, "Close the Palworld game completely."
            if s:
                return BAD, "Close the server window (the black window). Make sure everyone has left first."
            return OK, "Everything is closed. Safe to go on!"

        def check_moved():
            left = host_left_behind(self._server_world())
            if left is None:
                return BAD, "I can't find your world on the server."
            return (BAD, "Your old character hasn't been moved yet.") if left else \
                   (OK, "Your old character has been moved!")

        return [
            Step("Find your server",
                 "The server is the program your friends connect to. Pick the folder that has "
                 "PalServer.exe in it. If you installed it with Steam, it's usually\n"
                 "   <your Steam library>\\steamapps\\common\\PalServer",
                 check_server, [("Pick the server folder...", browse_server)]),
            Step("Put your world on the server",
                 "Your co-op world has to be copied into the server's save folder, and the server has to be "
                 "told to use it. I can do both for you.",
                 check_world_on_server,
                 [("Do it for me", do_copy),
                  ("Open my world folder", lambda: self._open(self.world and self.world.folder.parent)),
                  ("Open server settings", lambda: self.server and self._open(self.server.ini))],
                 self._world_picker),
            Step("Make a throw-away character",
                 f"1. Start the server.\n"
                 f"2. Start Palworld, pick 'Join Multiplayer Game' and type {SERVER_ADDRESS}\n"
                 "3. The game will make you a NEW character. Name it DELETE ME.\n"
                 "4. Play for about a minute, then quit to the title screen.\n\n"
                 "Don't worry, your real character comes back in a moment.",
                 check_new_char,
                 [("Start the server", self._start_server),
                  ("Copy server address", lambda: self._copy(SERVER_ADDRESS, "the server address"))]),
            Step("Close everything",
                 "Close the Palworld game and the server. If they're open while we fix things, "
                 "they'll undo our changes. Look at the top of this window: both should say closed or stopped.",
                 check_closed),
            Step("Bring your old character back",
                 "Click the button. The Transfer tab opens with your server's world loaded. Pick your OLD "
                 "character in Keep (it says CO-OP HOST) and the DELETE ME character in Replace. Then click "
                 "'Transfer character'. A backup is made first.",
                 check_moved, [("Open the Transfer tab", lambda: self._to_transfer(self._server_world()))]),
            Step("Check your map",
                 "Your explored map lives on YOUR PC, not the server. Joining the server can cover it up "
                 "again. Let's make sure it's OK.",
                 self._check_map, [("Fix my map", lambda: self._go(self.map_page, self.world))]),
            Step("Play!",
                 f"Start the server, open Palworld and join {SERVER_ADDRESS}. "
                 "You should be your old character, with your pals, items and map.\n\n"
                 "Friends join with your PC's address. They may need help with port 8211 on your router.",
                 None,
                 [("Start the server", self._start_server),
                  ("Copy server address", lambda: self._copy(SERVER_ADDRESS, "the server address"))]),
        ]

    def _check_map(self):
        if self.world is None:
            return INFO, "Pick your world in an earlier step."
        if game_running(running_programs()):
            return INFO, "Close Palworld so I can check your map."
        if map_looks_covered(self.world.folder):
            return BAD, "Your map looks covered. Click 'Fix my map'."
        return OK, "Your map looks fine."

    # -- server on a friend's PC ----------------------------------------------
    def _friend_server_steps(self) -> list[Step]:
        def friend_note(w: LocalWorld):
            return ("Hi! Here's my Palworld world.\n"
                    "1. Stop the server.\n"
                    f"2. Put the folder {w.world_id} into  PalServer\\Pal\\Saved\\SaveGames\\0\\\n"
                    "3. Open PalServer\\Pal\\Saved\\Config\\WindowsServer\\GameUserSettings.ini and set\n"
                    f"   DedicatedServerName={w.world_id}\n"
                    "4. Start the server.")

        def send_world():
            if self.world:
                self._make_zip(self.world.folder, f"{self.world.name or 'Palworld world'} - for my friend.zip",
                               skip={"backup"})

        def check_picked():
            if self.world is None:
                return BAD, "Pick your world above."
            return INFO, (f"Send your friend the zip, plus the note (World ID {self.world.world_id}). "
                          "Skip this if your friend already has your world.")

        def check_joined():
            if game_running(running_programs()):
                return INFO, "When you've played about a minute, quit the game completely."
            return INFO, "Done? Go to the next step."

        def pick_returned():
            d = filedialog.askdirectory(title="Pick the world folder your friend sent back (it has Level.sav)")
            if d:
                p = Path(d)
                subs = list(p.glob("*/Level.sav"))
                if not (p / "Level.sav").is_file() and len(subs) == 1:
                    p = subs[0].parent
                self.returned = p
                self.recheck()

        def check_returned():
            if self.returned is None:
                return BAD, "Click 'Pick the folder' after your friend sends it. If it's a zip, unzip it first."
            if not (self.returned / "Level.sav").is_file():
                return BAD, "That folder has no Level.sav in it. Pick the folder with the World ID as its name."
            if self.world and self.returned.name.upper() != self.world.world_id.upper():
                return BAD, "That's a different world than the one you picked in step 1. Double-check it."
            left = host_left_behind(self.returned)
            n = len(new_characters(self.returned))
            if left and n:
                return OK, f"Got it! Your old character is in there, plus {n} new character(s)."
            if left:
                return BAD, "Your old character is there, but no new one yet. Did you join the server first?"
            return OK, "Your old character has already been moved in this folder."

        def check_moved():
            left = host_left_behind(self.returned)
            if left is None:
                return BAD, "Go back a step and pick the folder your friend sent."
            return (BAD, "Your old character hasn't been moved yet.") if left else \
                   (OK, "Your old character has been moved!")

        def send_back():
            if self.returned:
                self._make_zip(self.returned, f"{self.returned.name} - send back to friend.zip",
                               only={"Level.sav", "Players"})

        back_note = ("Here's the fixed world!\n"
                     "1. Stop the server.\n"
                     "2. In PalServer\\Pal\\Saved\\SaveGames\\0\\<WorldID>\\Players DELETE these two files:\n"
                     f"   {HOST_UID}.sav  and  {HOST_UID}_dps.sav\n"
                     "3. Replace Level.sav and the Players folder with the ones in this zip.\n"
                     "4. Start the server.")

        return [
            Step("Send your world to your friend",
                 "Pick your co-op world. I'll make a zip on your Desktop for your friend, and a note "
                 "telling them where to put it.",
                 check_picked,
                 [("Make the zip", send_world),
                  ("Copy the note for my friend", lambda: self.world and self._copy(friend_note(self.world),
                                                                                    "the note"))],
                 self._world_picker),
            Step("Join your friend's server once",
                 "1. Join your friend's server.\n"
                 "2. The game makes you a NEW character. Name it DELETE ME.\n"
                 "3. Play for about a minute, then quit to the title screen and close the game.",
                 check_joined),
            Step("Get the world back from your friend",
                 "Ask your friend to STOP the server and send you this folder:\n"
                 "   PalServer\\Pal\\Saved\\SaveGames\\0\\<WorldID>\n"
                 "Unzip it if it's a zip, then pick it here.",
                 check_returned, [("Pick the folder", pick_returned)]),
            Step("Bring your old character back",
                 "Click the button. Pick your OLD character in Keep (it says CO-OP HOST) and DELETE ME "
                 "in Replace. Then click 'Transfer character'. A backup is made first.",
                 check_moved, [("Open the Transfer tab", lambda: self._to_transfer(self.returned))]),
            Step("Send it back to your friend",
                 "I'll zip just the files your friend needs. Send them the zip and the note. The note "
                 "matters: two old files MUST be deleted on the server, or guild invites can break.",
                 check_moved,
                 [("Make the zip", send_back),
                  ("Copy the note for my friend", lambda: self._copy(back_note, "the note"))]),
            Step("Check your map",
                 "Your explored map lives on YOUR PC, not your friend's server. Joining the server can "
                 "cover it up again. Your friend can't fix this for you, but you can, right here.",
                 self._check_map, [("Fix my map", lambda: self._go(self.map_page, self.world))]),
            Step("Play!",
                 "Once your friend has started the server, join it. You should be your old character, "
                 "with your pals, items and map.",
                 None),
        ]

    # -- map fix ---------------------------------------------------------
    def map_page(self, world: LocalWorld | None):
        self.flow = []
        self._clear()
        self._title("Fix my map",
                    "Your map is saved on YOUR PC. When you join a server that uses the same world, the "
                    "game can save a blank map over your old one. The game keeps backups, so we can put "
                    "your old map back.")
        if not self.worlds:
            ttk.Label(self.body, text="Pick your world folder. It's inside "
                                      "%LOCALAPPDATA%\\Pal\\Saved\\SaveGames\\<your Steam ID>\\",
                      style="GText.TLabel", wraplength=820).pack(anchor="w")
            ttk.Button(self.body, text="Pick my world folder...", style="GBig.TButton",
                       command=lambda: self._browse_world(lambda: self.map_page(self.world))).pack(anchor="w", pady=8)
            return
        worlds = self.worlds
        world = next((w for w in (world, self.world) if w in worlds), worlds[0])
        ttk.Label(self.body, text="Which world?", style="GText.TLabel").pack(anchor="w")
        pick_row = ttk.Frame(self.body)
        pick_row.pack(anchor="w", fill="x", pady=(2, 8))
        cb = ttk.Combobox(pick_row, state="readonly", values=[w.label() for w in worlds], width=90)
        cb.current(worlds.index(world))
        cb.pack(side="left")
        ttk.Button(pick_row, text="Pick a different folder...",
                   command=lambda: self._browse_world(lambda: self.map_page(self.world))).pack(side="left", padx=6)

        ttk.Label(self.body, text="Pick a map to put back. A longer bar means more of the map is uncovered.",
                  style="GText.TLabel").pack(anchor="w")
        box = ttk.Frame(self.body)
        box.pack(fill="x", pady=4)
        tree = ttk.Treeview(box, columns=("when", "map", "what"), show="headings", height=9,
                            selectmode="browse")
        sb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        for col, text, width in (("when", "Saved", 170), ("map", "How much map", 260), ("what", "What it is", 330)):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="w")
        tree.pack(side="left", fill="x", expand=True)
        status = tk.Label(self.body, font=("Segoe UI", 12, "bold"), wraplength=820, justify="left")
        status.pack(anchor="w", pady=6)
        copies: list[MapCopy] = []

        def fill(_=None):
            nonlocal copies
            w = worlds[cb.current()]
            copies = map_copies(w.folder)
            tree.delete(*tree.get_children())
            biggest = max((c.size for c in copies), default=1) or 1
            best = best_map(copies)
            for i, c in enumerate(copies):
                bar = "\u2588" * max(1, round(20 * c.size / biggest))
                note = c.note + ("   \u2190 best pick" if c is best else "")
                tree.insert("", "end", iid=str(i), values=(c.when.strftime("%b %d, %I:%M %p"), bar, note))
            if best is not None:
                tree.selection_set(str(copies.index(best)))
            if not copies:
                status.configure(text="No map saves found for this world.", fg=COLORS[BAD])
            elif map_looks_covered(w.folder):
                status.configure(text="\u26a0 Your current map looks covered. The best pick is "
                                      "selected for you.", fg=COLORS[BAD])
            else:
                status.configure(text="\u2714 Your current map looks fine. You only need this if something's "
                                      "still missing.", fg=COLORS[OK])
        cb.bind("<<ComboboxSelected>>", fill)
        fill()

        def put_back():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("Guide", "Click a map in the list first.")
                return
            c, w = copies[int(sel[0])], worlds[cb.current()]
            if not self._confirm_closed("Palworld"):
                return
            if not messagebox.askyesno("Guide", f"Put back the map from {c.when:%b %d, %I:%M %p}?\n\n"
                                                "Your current map is saved first, so you can undo this."):
                return

            def done(_):
                fill()
                messagebox.showinfo("Guide", "Your map is back! Start Palworld and join the server.\n\n"
                                             "Changed your mind? Pick 'saved before an earlier fix' in the "
                                             "list to undo.")
            self.app.run_bg(lambda: restore_map(w.folder, c.path, self.app.log), done)

        row = ttk.Frame(self.body)
        row.pack(anchor="w", pady=6)
        ttk.Button(row, text="Put this map back", style="GBig.TButton", command=put_back).pack(side="left")
        ttk.Label(self.body, text="Close Palworld before you click. If the game is open, it saves over the map.",
                  style="GSmall.TLabel").pack(anchor="w")
