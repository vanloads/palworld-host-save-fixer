# Palworld Host Save Fixer

**Moved your co-op world to a dedicated server and the host had to make a new
character? This puts the old one back.**

A small Windows app: pick the world folder, pick your old and new character,
click Transfer. It works with current Palworld saves, including 1.0 and the
Oodle-compressed (`PlM`) format that breaks most older fix scripts.

**[Download the latest PalHostFix.exe from Releases](../../releases/latest)**.
There's nothing to install.

> Tested end-to-end: a co-op world was moved to a Palworld Dedicated Server,
> and the host came back as their old character with their pals, inventory,
> Dimensional Pal Storage and guild admin rights.

---

## Why the host loses their character

A co-op world always saves the host under the placeholder ID
`00000000-0000-0000-0000-000000000001`. Everyone who joins gets an ID based
on their Steam account. A dedicated server has no host, so you join with your
Steam-based ID like everyone else. The server finds no save under that ID and
makes you a new character. Your friends were fine because their IDs were
already Steam-based.

## How to use it

1. Copy the co-op world folder to the server. It's in
   `%LOCALAPPDATA%\Pal\Saved\SaveGames\<SteamID>\<WorldID>`. Set
   `DedicatedServerName=<WorldID>` in the server's `GameUserSettings.ini`.
2. Join the server once and create a throw-away character. Stay about a
   minute, then **quit to the title screen**.
3. **Stop the server** and download its world folder:
   `Pal/Saved/SaveGames/0/<WorldID>/`.
4. Open **PalHostFix.exe** and load that folder.
   - **Keep:** your old character, marked `CO-OP HOST`.
   - **Replace:** the throw-away character.
5. Click **Preview changes**, then **Transfer character**. A `.zip` backup of
   the whole world is made first.
6. Upload `Level.sav` and the `Players` folder back to the server. Make sure
   `Players/00000000000000000000000000000001.sav` and its `_dps.sav` are gone
   there. Then start the server.

The **Why / How-to** tab in the app has the full checklist. The **Extras** tab
has:
- **Copy map exploration:** brings your fog of war over from the co-op world.
  It's stored on your PC, not the server.
- **Restore a backup.**
- **ID scan:** diagnostics showing every place an ID appears in Level.sav.

## What it changes

| Where | What |
|---|---|
| `Players/<old>.sav` | Becomes `Players/<new>.sav`, with the ID fields inside updated |
| `Players/<old>_dps.sav` | Becomes `<new>_dps.sav`, with pal ownership updated (Dimensional Pal Storage) |
| Level.sav: your character | Re-keyed to the new ID |
| Level.sav: your pals | Owner, previous-owner and nickname-editor fields updated |
| Level.sav: guild | Member entry, admin and character handle updated |
| Level.sav: buildings | "Built by" updated on structures you built |
| Level.sav: throw-away character | Removed, along with its pals, its inventory, and its guild if it made one |

## Why other fixes often fail

* **New compression.** Since 1.0, saves use Oodle (`PlM`), and older tools
  reject them.
* **`_dps.sav` is skipped.** Dimensional Pal Storage is a separate file, and
  many tools leave it behind.
* **The 1.0 guild record changed.** Hard-coded parsers break on it.
* **Find-and-replace of the host ID corrupts saves.** The host ID is twelve
  zero bytes and a `1`, so it matches by coincidence thousands of times in
  item and map data. Every co-op pal's map key also carries it, even pals
  that belong to your friends.

This app reads the save structure and records where each field sits. It
patches only the specific fields, and it fixes every enclosing size and count.
Before writing, it re-parses the result and checks that the whole file still
parses completely.

## FAQ

**The app says a player file is "nothing but zeros" or "cut short".** The file
was copied while the server was writing it, or the download from your host
failed. Download again with the server stopped. If it's only the throw-away
character's file, the app works around it.

**Does it work for Xbox / Game Pass saves?** Not tested. It is built for
Steam co-op worlds moving to a Steam dedicated server.

**The server ignores my PalWorldSettings.ini.** Delete `WorldOption.sav` from
the world folder; it overrides the ini.

**A pal won't follow or fight.** Put it in the palbox and take it out again.

## Building from source

Requires Python 3.10+.

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python PalHostFix.pyw
```

`build_exe.bat` produces `dist\PalHostFix.exe` with PyInstaller.

The app reads Oodle saves with [pyooz](https://pypi.org/project/pyooz/). It
writes zlib (`PlZ`) saves, which the game and server load and convert back on
their next autosave.

## License & credits

GPL-3.0. See [LICENSE](LICENSE).

The save format knowledge builds on community research by
[cheahjs/palworld-save-tools](https://github.com/cheahjs/palworld-save-tools),
[xNul/palworld-host-save-fix](https://github.com/xNul/palworld-host-save-fix)
and others. This project's code is independent.

Not affiliated with or endorsed by Pocketpair. Always keep backups.
