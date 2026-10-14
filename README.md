# Palworld Host Save Fixer

*Created by **vanloads***

**Moved your co-op world to a dedicated server and the host had to make a new
character? This puts the old one back.**

A small Windows app: pick the world folder, pick your old and new character,
click Transfer. It works with current Palworld saves, including 1.0 and the
Oodle-compressed (`PlM`) format that breaks most older fix scripts.

**[Download the latest PalHostFix.exe from Releases](../../releases/latest)**.
There's nothing to install. Open it and the **Start here (Guide)** tab walks
you through everything, one step at a time.

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
  It's stored on your PC, not the server. If a friend now hosts the server,
  see [Your map is covered again](#your-map-is-covered-again-a-friend-hosts-the-server).
- **Restore a backup.**
- **ID scan:** diagnostics showing every place an ID appears in Level.sav.

## Your map is covered again (a friend hosts the server)

This is for you if you hosted the co-op world, then gave the world folder to a
friend to run as a dedicated server. Your character came back, but the map
is covered again, or only partly uncovered.

**Your map isn't on the server.** Each player's map exploration is saved on
their own PC, in `LocalData.sav`. The server never stores it or sends it to
anyone. Your friend can't fix your map from the server, and sending them
server files won't fix it either. Only the old host has this problem. Players
who joined your co-op world already have their map on their own PC.

**Why your map got covered.** The server keeps the same World ID, so when you
join it, your PC saves your map into the **same folder as your old co-op
world**:

```
%LOCALAPPDATA%\Pal\Saved\SaveGames\<YourSteamID>\<WorldID>\LocalData.sav
```

The first time you join, the game saves the server's mostly blank map over
your old one. You won't see a new folder for the server, and **From** and
**To** in *Copy map exploration* would be the same folder, so the copy can't
help here.

**How to get it back.** The game makes its own backups, so your old map
is still there.

1. Fully exit Palworld. If it's still running, it will save over your fix.
2. Open `%LOCALAPPDATA%\Pal\Saved\SaveGames\<YourSteamID>\<WorldID>\`.
3. Copy `LocalData.sav` to somewhere safe, just in case.
4. Open `backup\local\`. Each folder is named with a date and time. Pick the
   **last one before you first joined the server**, which is usually your
   last co-op session.
5. Copy the `LocalData.sav` from that folder back into `<WorldID>\`,
   replacing the current one.
6. Start Palworld and join the server. Your old map is back.

Can't find the right backup? The world folder you gave your friend has a copy
of your co-op map too: `<WorldID>\LocalData.sav`. The server ignores this
file, so it's still your untouched co-op map. Ask your friend for that one
file and use it in step 5.

Anything you uncovered on the server before the fix gets covered again,
because the restored map is from your last co-op session.

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

## What this app looks at

The app never searches your PC. It reads only the folders you pick yourself
with a **Pick...** or **Browse...** button: your world folder, your server
folder, or the folder a friend sent you.

When it starts, it asks one question: may it check which programs are open?
If you say OK, it tells you when Palworld or the server still needs to be
closed before a change. If you say no, it asks you to confirm instead.

It changes files only when you click a button that says so, and it backs them
up first. It has no network code: it never connects to the internet and never
sends anything anywhere. The full source is in this repo, so you can check.

## Check your download

Each release lists the SHA-256 checksum of its files. To check yours, open
Command Prompt in the folder with the download and run:

```bat
certutil -hashfile PalHostFix.zip SHA256
```

The long code it prints should match the one in the release notes exactly.
If it doesn't, don't run it, and download it again from this page.

## Building from source

Requires Python 3.10+.

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python PalHostFix.pyw
```

`build_exe.bat` produces `dist\PalHostFix.exe` with PyInstaller, plus
`dist\PalHostFix.exe.sha256` with its checksum for the release notes.

The app reads Oodle saves with [pyooz](https://pypi.org/project/pyooz/). It
writes zlib (`PlZ`) saves, which the game and server load and convert back on
their next autosave.

## License & credits

Copyright (C) 2026 vanloads. Licensed under GPL-3.0; see [LICENSE](LICENSE).

The save format knowledge builds on community research by
[cheahjs/palworld-save-tools](https://github.com/cheahjs/palworld-save-tools),
[xNul/palworld-host-save-fix](https://github.com/xNul/palworld-host-save-fix)
and others. This project's code is independent.

Not affiliated with or endorsed by Pocketpair. Always keep backups.
