# Palworld Host Save Fixer - created by vanloads
# Licensed under GPL-3.0 (see LICENSE)
"""Tkinter front end for the Palworld host save fixer."""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .gvas import fmt_guid, parse_gvas
from .guide import GuideTab
from .savfile import read_sav
from .world import (HOST_UID, TransferError, World, copy_local_data, norm_uid,
                    restore_backup, scan_uid)

from . import __version__

APP_TITLE = f"Palworld Host Save Fixer v{__version__}  -  by vanloads"

GUIDE = """\
Palworld Host Save Fixer - created by vanloads

WHY YOUR CHARACTER DISAPPEARED
In a co-op world the host is always saved under the placeholder ID
00000000-0000-0000-0000-000000000001. Everyone who joins gets an ID derived
from their Steam account. A dedicated server has no host, so you join with
your Steam-derived ID like everyone else. The server finds no save under that
ID and makes you a new character. Your friends were fine because their IDs
were already Steam-derived.

This tool moves the old host character (Players/000...0001.sav plus its
Dimensional Pal Storage file) to the ID the server gave you. It also updates
everything in Level.sav that points at you: your pals' ownership, guild
membership and admin rights, and the "built by" tag on your structures. Then
it removes the throw-away character the server created.

STEP BY STEP
 1. Upload the co-op world to the server, then join it once yourself and
    create the throw-away character. Pick a name you'll recognise
    (e.g. "DELETE ME"). Your friends can play as normal.
 2. STOP the server. Edits made while it runs get overwritten.
 3. Download the server's world folder:
      <server>/Pal/Saved/SaveGames/0/<WorldID>/
    It holds Level.sav and a Players folder.
 4. Open that folder here (step 1 on the Transfer tab).
 5. Under "Keep", pick your old character (marked CO-OP HOST).
    Under "Replace", pick the new character the server made for you.
 6. Click Preview to check the plan, then click Transfer. A .zip backup of
    the whole world is made next to the folder first.
 7. Upload Level.sav and the whole Players folder back to the server.
    Make sure Players/00000000000000000000000000000001.sav and
    ..._dps.sav are GONE on the server. A leftover host file can block
    guild invites.
 8. Start the server and join.

EXTRA TIPS
 - Map exploration (fog of war) is stored on YOUR PC, not the server. Use
   Extras > "Copy map exploration" to bring it over from the co-op world.
 - If the server ignores PalWorldSettings.ini, delete WorldOption.sav from
   the world folder. It overrides the ini.
 - DedicatedServerName in GameUserSettings.ini must match the world folder
   name exactly, or the server starts a brand-new world.
 - If a pal won't follow or fight after moving, put it in the palbox and take
   it out again.
 - The tool writes zlib (PlZ) saves. The game and server load them fine and
   convert them back to their own format on the next autosave.
"""


def find_worlds() -> list[tuple[str, Path]]:
    """Find world folders on this PC: co-op saves and local dedicated servers."""
    roots = []
    la = os.environ.get("LOCALAPPDATA")
    if la:
        roots.append(Path(la) / "Pal" / "Saved" / "SaveGames")
    libs = [Path(r"C:\Program Files (x86)\Steam\steamapps")]
    vdf = libs[0] / "libraryfolders.vdf"
    try:
        for line in vdf.read_text(errors="ignore").splitlines():
            if '"path"' in line:
                libs.append(Path(line.split('"')[3].replace("\\\\", "\\")) / "steamapps")
    except OSError:
        pass
    for lib in libs:
        roots.append(lib / "common" / "PalServer" / "Pal" / "Saved" / "SaveGames")
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for lvl in root.glob("*/*/Level.sav"):
            folder = lvl.parent
            name = folder.name
            try:
                data, _ = read_sav(folder / "LevelMeta.sav")
                _, r = parse_gvas(data)
                wn = r.path("SaveData", "WorldName")
                if wn is not None and wn.value:
                    name = f"{wn.value}  ({folder.name[:8]}...)"
            except Exception:
                pass
            kind = "server" if "PalServer" in str(folder) else "co-op"
            found.append((f"[{kind}] {name}", folder))
    return found


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("960x820")
        self.minsize(760, 560)
        self.world: World | None = None
        self.choices = []
        self.msgs: queue.Queue = queue.Queue()
        self.busy = False
        self._build()
        self.after(100, self._pump)
        # The Guide tab runs _refresh_local_worlds once the user says it may look.

    # -- layout ----------------------------------------------------------
    def _build(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Head.TLabel", font=("Segoe UI", 11, "bold"))
        nb = self.nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.tab_main = ttk.Frame(nb, padding=10)
        self.tab_extra = ttk.Frame(nb, padding=10)
        self.tab_help = ttk.Frame(nb, padding=10)
        self.tab_guide = GuideTab(nb, self)
        nb.add(self.tab_guide, text="Start here (Guide)")
        nb.add(self.tab_main, text="Transfer")
        nb.add(self.tab_extra, text="Extras")
        nb.add(self.tab_help, text="Why / How-to")
        self._build_main(self.tab_main)
        self._build_extra(self.tab_extra)
        txt = ScrolledText(self.tab_help, wrap="word", font=("Segoe UI", 10))
        txt.insert("1.0", GUIDE)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)

        logf = ttk.LabelFrame(self, text="Log", padding=4)
        logf.pack(fill="both", expand=False, padx=8, pady=8)
        self.log_box = ScrolledText(logf, height=10, wrap="word", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")

    def _build_main(self, f):
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="1. World folder (contains Level.sav and Players)", style="Head.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w")
        self.folder_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.folder_var).grid(row=1, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(f, text="Browse...", command=self._browse).grid(row=1, column=2, padx=(6, 0))
        ttk.Label(f, text="or found on this PC:").grid(row=2, column=0, sticky="w")
        self.local_var = tk.StringVar()
        self.local_cb = ttk.Combobox(f, textvariable=self.local_var, state="readonly")
        self.local_cb.grid(row=2, column=1, sticky="ew", pady=2)
        self.local_cb.bind("<<ComboboxSelected>>", self._pick_local)
        ttk.Button(f, text="Load", command=self._load).grid(row=2, column=2, padx=(6, 0))
        self.world_info = ttk.Label(f, text="No world loaded.", foreground="#555")
        self.world_info.grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 10))

        ttk.Label(f, text="2. Choose characters", style="Head.TLabel").grid(row=4, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Keep (your old character):").grid(row=5, column=0, sticky="w", pady=4)
        self.old_var = tk.StringVar()
        self.old_cb = ttk.Combobox(f, textvariable=self.old_var)
        self.old_cb.grid(row=5, column=1, columnspan=2, sticky="ew")
        ttk.Label(f, text="Replace (new server character):").grid(row=6, column=0, sticky="w", pady=4)
        self.new_var = tk.StringVar()
        self.new_cb = ttk.Combobox(f, textvariable=self.new_var)
        self.new_cb.grid(row=6, column=1, columnspan=2, sticky="ew")
        ttk.Label(f, foreground="#555", wraplength=760, justify="left",
                  text="Your old character ends up under the new character's ID, so the server "
                       "recognises you. The new character is deleted. If you know your server ID "
                       "but haven't joined yet, you can type it into the Replace box.").grid(
            row=7, column=0, columnspan=3, sticky="w")

        ttk.Label(f, text="3. Options", style="Head.TLabel").grid(row=8, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.opt_clean = tk.BooleanVar(value=True)
        self.opt_build = tk.BooleanVar(value=True)
        self.opt_backup = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, variable=self.opt_clean,
                        text="Also delete the throw-away character's pals and inventory (recommended)").grid(
            row=9, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(f, variable=self.opt_build,
                        text="Update \"built by\" on structures you built").grid(row=10, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(f, variable=self.opt_backup,
                        text="Zip a backup of the world folder first (strongly recommended)").grid(
            row=11, column=0, columnspan=3, sticky="w")

        bar = ttk.Frame(f)
        bar.grid(row=12, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(bar, text="Preview changes", command=self._preview).pack(side="left", padx=4)
        self.go_btn = ttk.Button(bar, text="Transfer character", command=self._transfer)
        self.go_btn.pack(side="left", padx=4)

    def _build_extra(self, f):
        f.columnconfigure(1, weight=1)
        box = ttk.LabelFrame(f, text="Copy map exploration (LocalData.sav)", padding=8)
        box.grid(row=0, column=0, columnspan=3, sticky="ew")
        box.columnconfigure(1, weight=1)
        ttk.Label(box, wraplength=780, justify="left",
                  text="Your map's fog of war is saved on YOUR PC, per world. Copy it from the old co-op "
                       "world into the server's world folder on your PC. Join the server once first so "
                       "that folder exists (it's under SaveGames\\<steamid>\\<some ID>), then quit the game "
                       "before copying.").grid(row=0, column=0, columnspan=3, sticky="w")
        self.ld_src = tk.StringVar()
        self.ld_dst = tk.StringVar()
        for i, (lbl, var) in enumerate((("From (co-op world):", self.ld_src), ("To (server world):", self.ld_dst)), 1):
            ttk.Label(box, text=lbl).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Entry(box, textvariable=var).grid(row=i, column=1, sticky="ew")
            ttk.Button(box, text="Browse...", command=lambda v=var: self._browse_into(v)).grid(row=i, column=2, padx=(6, 0))
        ttk.Button(box, text="Copy", command=self._copy_local).grid(row=3, column=2, sticky="e", pady=(6, 0))

        box2 = ttk.LabelFrame(f, text="Restore a backup", padding=8)
        box2.grid(row=1, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(box2, text="Put the loaded world folder back the way a backup .zip left it. "
                             "The current state is backed up first.").pack(side="left")
        ttk.Button(box2, text="Choose backup...", command=self._restore).pack(side="right")

        box3 = ttk.LabelFrame(f, text="Diagnostics", padding=8)
        box3.grid(row=2, column=0, columnspan=3, sticky="ew")
        box3.columnconfigure(1, weight=1)
        ttk.Label(box3, text="Find every place an ID appears in Level.sav:").grid(row=0, column=0, sticky="w")
        self.scan_var = tk.StringVar(value=fmt_guid(HOST_UID))
        ttk.Entry(box3, textvariable=self.scan_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(box3, text="Scan", command=self._scan).grid(row=0, column=2)
        ttk.Label(box3, foreground="#555", wraplength=780, justify="left",
                  text="Raw byte matches. For 000...0001 most hits are coincidences (it's just zeros and "
                       "a 1), which is why this tool edits fields by structure instead of find-and-replace."
                  ).grid(row=1, column=0, columnspan=3, sticky="w")

    # -- helpers ---------------------------------------------------------
    def log(self, msg):
        self.msgs.put(("log", str(msg)))

    def _pump(self):
        try:
            while True:
                kind, payload = self.msgs.get_nowait()
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", payload + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def ui(self, fn):
        self.msgs.put(("call", fn))

    def run_bg(self, fn, done=None):
        if self.busy:
            messagebox.showinfo(APP_TITLE, "Still working on the previous task...")
            return
        self.busy = True
        self.configure(cursor="watch")

        def worker():
            try:
                res = fn()
                if done:
                    self.ui(lambda: done(res))
            except (TransferError, ValueError) as e:
                msg = str(e)
                self.log(f"ERROR: {msg}")
                self.ui(lambda: messagebox.showerror(APP_TITLE, msg))
            except Exception as e:
                self.log(traceback.format_exc())
                msg = f"{type(e).__name__}: {e}"
                self.ui(lambda: messagebox.showerror(APP_TITLE, msg))
            finally:
                def fin():
                    self.busy = False
                    self.configure(cursor="")
                self.ui(fin)

        threading.Thread(target=worker, daemon=True).start()

    def _browse(self):
        d = filedialog.askdirectory(title="Select the world folder (contains Level.sav)")
        if d:
            self.folder_var.set(d)
            self._load()

    def _browse_into(self, var):
        d = filedialog.askdirectory()
        if d:
            var.set(d)

    def _refresh_local_worlds(self):
        def job():
            return find_worlds()

        def done(worlds):
            self._local = worlds
            self.local_cb["values"] = [w[0] for w in worlds]
            if worlds:
                self.log(f"Found {len(worlds)} world(s) on this PC (see the drop-down).")
        self.run_bg(job, done)

    def _pick_local(self, _=None):
        i = self.local_cb.current()
        if i >= 0:
            self.folder_var.set(str(self._local[i][1]))
            self._load()

    def _load(self):
        folder = self.folder_var.get().strip().strip('"')
        if not folder:
            return
        p = Path(folder)
        if not (p / "Level.sav").is_file():
            # Accept the parent (SaveGames/0) or Level.sav itself.
            if p.is_file() and p.name.lower() == "level.sav":
                p = p.parent
            else:
                subs = [s for s in p.glob("*/Level.sav")] if p.is_dir() else []
                if len(subs) == 1:
                    p = subs[0].parent
            self.folder_var.set(str(p))

        def job():
            return World(p, log=self.log)

        def done(w: World):
            self.world = w
            self.choices = w.players()
            labels = [c.label() for c in self.choices]
            self.old_cb["values"] = labels
            self.new_cb["values"] = labels
            self.old_var.set("")
            self.new_var.set("")
            host = [c for c in self.choices if c.uid == HOST_UID]
            if host:
                self.old_var.set(host[0].label())
            others = [c for c in self.choices if c.uid != HOST_UID and c.has_character]
            fresh = [c for c in others if c.level <= 5]
            if host and len(fresh) == 1:
                # A brand-new, low-level character next to the host is almost
                # certainly the one the server just made.
                self.new_var.set(fresh[0].label())
                self.log(f"Guessed the new character is '{fresh[0].name}' (Lv {fresh[0].level}). Check it!")
            elif host:
                self.log("Pick the new character the server created for you in the 'Replace' box.")
            gs = [g for g in w.guilds if g.is_guild]
            self.world_info.configure(
                text=f"{w.folder}   -   {len([c for c in self.choices if c.has_character])} players, "
                     f"{len(w.characters)} characters, {len(gs)} guild(s)")
            self.log("Players in this world:")
            for c in self.choices:
                self.log(f"   {c.label()}" + (f"   [guild: {c.guild}]" if c.guild else ""))
            if not host:
                self.log("No co-op host (000...0001) character here. If this is the server's world, "
                         "check that the co-op world was copied over, or pick the old character manually.")
        self.run_bg(job, done)

    def _uid_from(self, text: str) -> str:
        for c in self.choices:
            if c.label() == text:
                return c.uid
        return norm_uid(text)

    def _make_plan(self):
        if self.world is None:
            raise TransferError("Load a world folder first.")
        old = self._uid_from(self.old_var.get())
        new = self._uid_from(self.new_var.get())
        return self.world.plan_transfer(old, new, clean_new=self.opt_clean.get(),
                                        fix_buildings=self.opt_build.get())

    def _preview(self):
        def job():
            plan = self._make_plan()
            self.log(plan.describe())
            return plan
        self.run_bg(job)

    def _transfer(self):
        try:
            if self.world is None:
                raise TransferError("Load a world folder first.")
            old = self._uid_from(self.old_var.get())
            new = self._uid_from(self.new_var.get())
        except (TransferError, ValueError) as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        name = lambda u: next((c.name for c in self.choices if c.uid == u and c.name), fmt_guid(u))
        if not messagebox.askyesno(
                APP_TITLE,
                f"Move '{name(old)}' to ID {fmt_guid(new)} and delete '{name(new)}'?\n\n"
                "Make sure the server (or game) is NOT running with this world.\n"
                f"Backup first: {'yes' if self.opt_backup.get() else 'NO'}"):
            return

        def job():
            plan = self._make_plan()
            self.log(plan.describe())
            bk = plan.apply(backup=self.opt_backup.get(), log=self.log)
            return bk

        def done(bk):
            self.log("Done! " + (f"Backup: {bk}" if bk else ""))
            messagebox.showinfo(APP_TITLE, "Transfer complete.\n\nUpload Level.sav and the Players folder "
                                           "back to the server, and make sure the 000...0001 files are gone "
                                           "there." + (f"\n\nBackup saved to:\n{bk}" if bk else ""))
            self._load()
        self.run_bg(job, done)

    def _copy_local(self):
        src, dst = self.ld_src.get().strip(), self.ld_dst.get().strip()
        if not src or not dst:
            messagebox.showinfo(APP_TITLE, "Choose both folders first.")
            return
        self.run_bg(lambda: copy_local_data(Path(src), Path(dst), self.log),
                    lambda _: messagebox.showinfo(APP_TITLE, "Map exploration copied."))

    def _restore(self):
        if self.world is None:
            messagebox.showinfo(APP_TITLE, "Load the world folder you want to restore first.")
            return
        z = filedialog.askopenfilename(title="Choose a backup zip", initialdir=str(self.world.folder.parent),
                                       filetypes=[("Backup zip", "*.zip")])
        if not z:
            return
        if not messagebox.askyesno(APP_TITLE, f"Replace {self.world.folder.name} with {Path(z).name}?"):
            return
        folder = self.world.folder
        self.run_bg(lambda: restore_backup(Path(z), folder, self.log), lambda _: self._load())

    def _scan(self):
        if self.world is None:
            messagebox.showinfo(APP_TITLE, "Load a world first.")
            return
        uid = self.scan_var.get()

        def job():
            w = self.world
            full = World.__new__(World)
            full.__dict__.update(w.__dict__)
            full.header, full.root = parse_gvas(w.data)  # parse everything
            rows = scan_uid(full, uid)
            self.log(f"Occurrences of {uid} in Level.sav:")
            for path, n in rows:
                self.log(f"  {n:6d}  {path}")
            if not rows:
                self.log("  none")
        self.run_bg(job)


def main():
    App().mainloop()
