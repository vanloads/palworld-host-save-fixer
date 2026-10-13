# Palworld Host Save Fixer - created by vanloads
# Licensed under GPL-3.0 (see LICENSE)
"""Palworld world model and the host-character transfer.

Why the host loses their character
----------------------------------
In a co-op (listen-server) world the host is always stored under the fixed
placeholder PlayerUId ``00000000-0000-0000-0000-000000000001``.  Everyone else
gets a PlayerUId derived from their Steam account.  A dedicated server has no
"host": every player, including you, gets the Steam-derived ID.  So the server
looks for ``Players/<your steam-derived id>.sav``, finds nothing and makes a
new character, while the real one sits orphaned under ``...0001``.

Friends are unaffected because their ID was already Steam-derived.

What the transfer does
----------------------
Given OLD (the character to keep, normally ``...0001``) and NEW (the ID the
server gave you, i.e. the fresh character):

1. Removes NEW's throw-away character from Level.sav (character entry, the pals
   and item containers that only it could reach, its guild membership, and its
   own guild if it made one).
2. Re-keys OLD's player character to NEW and re-points ownership of OLD's pals
   (OwnerPlayerUId / OldOwnerPlayerUIds / LastNickNameModifierPlayerUid),
   guild membership (member list, admin, character handle) and building
   ownership (build_player_uid) from OLD to NEW.
3. Writes Players/OLD.sav as Players/NEW.sav (with the IDs inside fixed) and
   does the same for OLD_dps.sav (Dimensional Pal Storage).

Things it deliberately does NOT touch: in co-op saves *every* pal's map key
also carries ``...0001`` (it is the world owner, not the pal owner), and so do
container slots and work assignments.  Friends' pals have the same keys and
load fine on the server, so they are left alone.  A blind byte-replace of
``...0001`` - what simpler tools do - would also hit thousands of unrelated
bytes (that pattern is just twelve zero bytes and a 1).
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .gvas import (Node, Patcher, bytes_to_guid, fmt_guid, guid_to_bytes,
                   ParseError, parse_blob, parse_gvas, structure_check)
from .savfile import SaveFormatError, read_sav, write_sav

HOST_UID = "00000000000000000000000000000001"
ZERO_UID = "0" * 32

# Large sections the transfer never needs to look inside.
LEVEL_SKIP = {
    "FoliageGridSaveDataMap", "MapObjectSpawnerInStageSaveData",
    "DynamicItemSaveData", "WorkSaveData", "EnemyCampSaveData",
    "DungeonSaveData", "DungeonPointMarkerSaveData", "LockGimmickSaveData",
}

# Map object Model.RawData layout (stable from 0.1 through 1.0):
# instance_id, concrete_model_instance_id, base_camp_id_belong_to,
# group_id_belong_to (4 x guid), hp (2 x i32), transform (10 x double),
# repair_work_id, owner_spawner_level_object_instance_id, owner_instance_id,
# build_player_uid ...
MODEL_GROUP_OFFSET = 48
MODEL_BUILDER_OFFSET = 200


class TransferError(Exception):
    pass


def norm_uid(text: str) -> str:
    h = re.sub(r"[^0-9a-fA-F]", "", text or "").upper()
    if len(h) != 32:
        raise ValueError(f"'{text}' is not a 32-hex-digit player ID")
    return h


@dataclass
class Character:
    uid: str
    instance_id: str
    entry: Node              # CharacterSaveParameterMap entry
    blob: Node               # parsed RawData
    is_player: bool
    name: str = ""
    level: int = 0
    char_id: str = ""        # pal species
    owner: str = ""
    container_id: str = ""   # SlotId.ContainerId.ID
    guild_id: str = ""       # trailing group id after the property list


@dataclass
class GuildMember:
    uid: str
    name: str
    start: int   # absolute offset of the entry
    end: int


@dataclass
class Guild:
    group_id: str
    group_type: str
    entry: Node
    raw: Node                 # RawData byte array node
    start: int                # absolute blob start
    length: int
    handles_count_off: int = 0
    handles: list = field(default_factory=list)  # (abs offset, uid, instance)
    members: list = field(default_factory=list)  # GuildMember
    members_count_off: int = -1
    member_extra: int = 0     # bytes following each member's name (1.0+: 1)
    admin_off: int = -1
    admin_uid: str = ""
    name: str = ""
    warnings: list = field(default_factory=list)

    @property
    def is_guild(self):
        return self.group_type.endswith("::Guild") or "IndependentGuild" in self.group_type


@dataclass
class PlayerFile:
    uid: str
    path: Path
    dps_path: Path | None
    name: str = ""
    containers: dict = field(default_factory=dict)  # role -> container id


@dataclass
class PlayerChoice:
    uid: str
    name: str
    level: int
    has_character: bool
    has_file: bool
    has_dps: bool
    guild: str

    def label(self) -> str:
        bits = [self.name or "(no character in world)"]
        if self.level:
            bits.append(f"Lv {self.level}")
        bits.append(fmt_guid(self.uid))
        if self.uid == HOST_UID:
            bits.append("CO-OP HOST")
        if not self.has_file:
            bits.append("no Players/.sav")
        return "  |  ".join(bits)


def _fstring_bytes(s: str) -> bytes:
    try:
        b = s.encode("ascii") + b"\0"
        return struct.pack("<i", len(b)) + b
    except UnicodeEncodeError:
        b = s.encode("utf-16-le") + b"\0\0"
        return struct.pack("<i", -(len(b) // 2)) + b


def _read_fstring(data, off, limit):
    """Strictly validated FString at ``off``; returns (text, end) or None."""
    if off + 4 > limit:
        return None
    n = struct.unpack_from("<i", data, off)[0]
    if 0 < n <= 256:
        end = off + 4 + n
        if end > limit or data[end - 1] != 0:
            return None
        b = bytes(data[off + 4:end - 1])
        if not b or any(c < 32 for c in b):
            return None
        return b.decode("latin-1"), end
    if -256 <= n < 0:
        end = off + 4 + (-n) * 2
        if end > limit or data[end - 2:end] != b"\0\0":
            return None
        txt = bytes(data[off + 4:end - 2]).decode("utf-16-le", errors="replace")
        if not txt or not txt.isprintable():
            return None
        return txt, end
    return None


class World:
    def __init__(self, folder: str | os.PathLike, log=print):
        self.folder = Path(folder)
        self.log = log
        self.level_path = self.folder / "Level.sav"
        self.players_dir = self.folder / "Players"
        if not self.level_path.is_file():
            raise TransferError(f"No Level.sav in {self.folder}")
        self.log(f"Reading {self.level_path} ...")
        self.data, self.level_info = read_sav(self.level_path)
        self.log(f"  {self.level_info.magic.decode()} save, {len(self.data):,} bytes uncompressed")
        self.header, self.root = parse_gvas(self.data, LEVEL_SKIP)
        self.ws = self.root.get("worldSaveData")
        if self.ws is None:
            raise TransferError("Level.sav has no worldSaveData")
        self.characters: list[Character] = []
        self.guilds: list[Guild] = []
        self.player_files: dict[str, PlayerFile] = {}
        self._load_characters()
        self._load_player_files()
        self._load_guilds()

    # -- loading ---------------------------------------------------------
    def _load_characters(self):
        cmap = self.ws.get("CharacterSaveParameterMap")
        for e in cmap.children:
            key, val = e.get("Key"), e.get("Value")
            raw = val.get("RawData") if val else None
            if key is None or raw is None or not isinstance(raw.value, tuple):
                continue
            uid = key.get("PlayerUId").value
            inst = key.get("InstanceId").value
            start, length = raw.value
            end = parse_blob(self.data, start, length, raw)
            sp = raw.get("SaveParameter")
            if sp is None:
                continue
            c = Character(uid, inst, e, raw, False)
            ip = sp.get("IsPlayer")
            c.is_player = bool(ip and ip.value)
            for attr, prop in (("name", "NickName"), ("level", "Level"), ("char_id", "CharacterID")):
                n = sp.get(prop)
                if n is not None and n.value is not None:
                    setattr(c, attr, n.value)
            if not c.level and c.is_player:
                c.level = 1
            o = sp.get("OwnerPlayerUId")
            c.owner = o.value if o is not None else ""
            cid = sp.path("SlotId", "ContainerId", "ID")
            c.container_id = cid.value if cid is not None else ""
            tail = self.data[end:start + length]
            if len(tail) >= 20:
                c.guild_id = bytes_to_guid(tail[4:20])
            self.characters.append(c)

    def _load_guilds(self):
        gmap = self.ws.get("GroupSaveDataMap")
        known = {c.uid for c in self.characters if c.is_player} | set(self.player_files)
        for e in gmap.children:
            val = e.get("Value")
            gt = val.get("GroupType") if val else None
            raw = val.get("RawData") if val else None
            if raw is None or not isinstance(raw.value, tuple):
                continue
            start, length = raw.value
            g = Guild(e.get("Key").value, gt.value if gt else "", e, raw, start, length)
            self._parse_guild_blob(g, known)
            self.guilds.append(g)

    def _parse_guild_blob(self, g: Guild, known_players: set[str]):
        d, s, lim = self.data, g.start, g.start + g.length
        pos = s + 16
        rs = _read_fstring(d, pos, lim)
        if rs is None:
            g.warnings.append("could not read group header")
            return
        pos = rs[1]
        g.handles_count_off = pos
        n = struct.unpack_from("<i", d, pos)[0]
        pos += 4
        if n < 0 or pos + 32 * n > lim:
            g.warnings.append("bad character handle list")
            return
        for i in range(n):
            o = pos + 32 * i
            g.handles.append((o, bytes_to_guid(d[o:o + 16]), bytes_to_guid(d[o + 16:o + 32])))
        tail = pos + 32 * n
        if not g.is_guild:
            return
        # The guild tail layout changes between game versions, so instead of
        # assuming fixed offsets, locate the member list by its content:
        # each member is <uid:16><last_online:i64><name:FString>[extra].
        found = []
        for uid in known_players:
            pat = guid_to_bytes(uid)
            i = d.find(pat, tail, lim)
            while i >= 0:
                fs = _read_fstring(d, i + 24, lim)
                if fs is not None:
                    found.append(GuildMember(uid, fs[0], i, fs[1]))
                i = d.find(pat, i + 1, lim)
        if not found:
            return
        found.sort(key=lambda m: m.start)
        # Gap between consecutive members = per-member trailing bytes.
        gaps = {b.start - a.end for a, b in zip(found, found[1:])}
        if len(gaps) > 1:
            g.warnings.append("member list is not contiguous; guild membership left untouched")
            return
        g.member_extra = gaps.pop() if gaps else self._guess_member_extra(found[0], lim)
        for m in found:
            m.end += g.member_extra
        count_off = found[0].start - 4
        count = struct.unpack_from("<i", d, count_off)[0]
        if count != len(found):
            g.warnings.append(f"member count {count} != {len(found)} recognised members "
                              "(a member may have no character in this world)")
        g.members = found
        g.members_count_off = count_off
        admin = bytes_to_guid(d[count_off - 16:count_off])
        if admin in {m.uid for m in found}:
            g.admin_off, g.admin_uid = count_off - 16, admin
        else:
            g.warnings.append("could not identify the guild admin field")
        # The guild display name is the FString immediately before an
        # optional 16-byte modifier id; it is informational only.
        g.name = self._find_guild_name(g, tail, count_off)

    def _guess_member_extra(self, m: GuildMember, lim: int) -> int:
        # Single member: no neighbour to measure against.  1.0 saves add one
        # byte (guild rank) after the name; earlier versions add nothing.
        return 1 if m.end < lim else 0

    def _find_guild_name(self, g: Guild, start: int, stop: int) -> str:
        d = self.data
        best = ""
        for i in range(start, max(start, stop - 4)):
            fs = _read_fstring(d, i, stop)
            if fs and len(fs[0]) >= 1 and fs[1] <= stop and fs[0].isprintable():
                best = fs[0]
        return best

    def _load_player_files(self):
        if not self.players_dir.is_dir():
            return
        for p in self.players_dir.glob("*.sav"):
            stem = p.stem
            if stem.lower().endswith("_dps"):
                continue
            try:
                uid = norm_uid(stem)
            except ValueError:
                continue
            dps = p.with_name(f"{stem}_dps.sav")
            self.player_files[uid] = PlayerFile(uid, p, dps if dps.is_file() else None)

    def player_file_details(self, uid: str) -> PlayerFile | None:
        pf = self.player_files.get(uid)
        if pf is None or pf.containers:
            return pf
        data, _ = read_sav(pf.path)
        _, root = parse_gvas(data)
        sd = root.get("SaveData")
        if sd is not None:
            for role in ("OtomoCharacterContainerId", "PalStorageContainerId"):
                n = sd.path(role, "ID")
                if n is not None:
                    pf.containers[role] = n.value
            inv = sd.get("InventoryInfo")
            if inv is not None:
                for c in inv.children:
                    n = c.get("ID")
                    if n is not None and n.value:
                        pf.containers[c.name] = n.value
        return pf

    # -- queries -----------------------------------------------------------
    def player_character(self, uid: str) -> Character | None:
        for c in self.characters:
            if c.is_player and c.uid == uid:
                return c
        return None

    def guild_of(self, uid: str) -> Guild | None:
        ch = self.player_character(uid)
        for g in self.guilds:
            if not g.is_guild:
                continue
            if any(m.uid == uid for m in g.members):
                return g
            if ch and any(h[1] == uid and h[2] == ch.instance_id for h in g.handles):
                return g
        return None

    def players(self) -> list[PlayerChoice]:
        uids = {c.uid for c in self.characters if c.is_player} | set(self.player_files)
        out = []
        for uid in uids:
            ch = self.player_character(uid)
            pf = self.player_files.get(uid)
            g = self.guild_of(uid)
            out.append(PlayerChoice(
                uid, ch.name if ch else "", ch.level if ch else 0, ch is not None,
                pf is not None, bool(pf and pf.dps_path), (g.name or fmt_guid(g.group_id)) if g else ""))
        out.sort(key=lambda p: (p.uid != HOST_UID, -p.level, p.name.lower()))
        return out

    # -- transfer --------------------------------------------------------
    def plan_transfer(self, old: str, new: str, clean_new=True, fix_buildings=True):
        """Build the edits.  Returns (TransferPlan).  Nothing is written."""
        old, new = norm_uid(old), norm_uid(new)
        if old == new:
            raise TransferError("Old and new character are the same ID.")
        if new == HOST_UID:
            raise TransferError("The NEW character can't be the co-op host ID: a dedicated "
                                "server never assigns 000...0001.  Swap the selections.")
        if new == ZERO_UID:
            raise TransferError("The new ID is all zeros, which is not a valid player ID.")
        old_char = self.player_character(old)
        if old_char is None:
            raise TransferError(f"No player character with ID {fmt_guid(old)} in Level.sav.")
        if old not in self.player_files:
            raise TransferError(f"Players/{old}.sav is missing - the old character's "
                                "inventory, tech and appearance live in that file.")

        plan = TransferPlan(self, old, new)
        P = plan.level
        new_char = self.player_character(new)
        removed_inst: set[str] = set()   # instance ids of deleted characters
        removed_keys: set[int] = set()   # entry.start of deleted map entries

        # 1. Remove the throw-away character created by the server.
        if new_char is not None:
            P.remove_element(new_char.entry)
            removed_keys.add(new_char.entry.start)
            removed_inst.add(new_char.instance_id)
            plan.note(f"Remove the server-created character '{new_char.name}' (Lv {new_char.level}).")
            pf_new = None
            if new in self.player_files:
                try:
                    pf_new = self.player_file_details(new)
                except (SaveFormatError, ParseError) as e:
                    # Only used to find the leftovers to tidy up; the file is
                    # overwritten with your old character either way.
                    plan.warn(f"The new character's own file is unreadable ({e}). That's OK: it will be "
                              "replaced. Its pals/inventory can't be identified, so they are left as "
                              "harmless unused data.")
            if clean_new and pf_new:
                own = set(pf_new.containers.values())
                pals = [c for c in self.characters if not c.is_player and c.container_id in own]
                for c in pals:
                    P.remove_element(c.entry)
                    removed_keys.add(c.entry.start)
                    removed_inst.add(c.instance_id)
                if pals:
                    plan.note(f"Remove {len(pals)} pal(s) carried by the new character "
                              f"({', '.join(sorted({p.char_id for p in pals}))}).")
                n = self._remove_containers(P, own)
                if n:
                    plan.note(f"Remove {n} inventory/pal container(s) that belonged to the new character.")
        elif new in self.player_files:
            plan.note("The new ID has a Players file but no character in Level.sav; it will be replaced.")
        else:
            plan.note("The new ID has never joined this world; the old character will simply be re-keyed.")

        # 2. Guild membership.
        old_guild = self.guild_of(old)
        new_guild = self.guild_of(new) if new_char is not None else None
        deleted = None
        if new_guild is not None and new_guild is not old_guild:
            if any(m.uid != new for m in new_guild.members):
                if new_guild.admin_uid == new:
                    raise TransferError(
                        f"The new character is the admin of guild '{new_guild.name}', which has other "
                        "members. Hand admin over (or leave that guild) in-game, then try again.")
                plan.note(f"Take the new character out of guild '{new_guild.name}'.")
            else:
                self._delete_guild(plan, new_guild)
                deleted = new_guild
        for g in self.guilds:
            if g is deleted:
                continue
            for off, uid, inst in g.handles:
                if inst in removed_inst:
                    self._remove_handle(P, g, off)
            for m in g.members:
                if m.uid == new and new_char is not None:
                    if not g.members_count_off > 0:
                        raise TransferError(f"Can't safely edit the member list of guild '{g.name}'.")
                    P.replace(m.start, m.end, b"", g.raw)
                    P.adjust_int(g.members_count_off, -1)
        if old_guild is not None:
            for off, uid, inst in old_guild.handles:
                if uid == old and inst == old_char.instance_id:
                    P.set_guid(off, new)
            moved = False
            for m in old_guild.members:
                if m.uid == old:
                    P.set_guid(m.start, new)
                    moved = True
            if not moved:
                plan.warn(f"Couldn't find you in the member list of guild '{old_guild.name}'; "
                          "you may need to be re-invited in-game.")
            if old_guild.admin_uid == old:
                P.set_guid(old_guild.admin_off, new)
                plan.note(f"Keep you as admin of guild '{old_guild.name}'.")
            plan.note(f"Move your membership of guild '{old_guild.name}' to the new ID.")
            for w in old_guild.warnings:
                plan.warn(f"Guild '{old_guild.name}': {w}")
        else:
            plan.warn("The old character isn't in a guild - nothing to move there.")

        # 3. The character itself and everything it owns.
        P.set_guid(old_char.entry.get("Key").get("PlayerUId").data_start, new)
        plan.note(f"Re-key '{old_char.name}' (Lv {old_char.level}) from {fmt_guid(old)} to {fmt_guid(new)}.")
        owned = 0
        for c in self.characters:
            if c.entry.start in removed_keys:
                continue
            hit = False
            for n in c.blob.walk():
                if n.struct_type == "Guid" and n.value == old and n.type == "StructProperty":
                    P.set_guid(n.end - 16, new)
                    hit = True
            if hit and not c.is_player:
                owned += 1
        plan.note(f"Transfer ownership of {owned} pal(s) in the world.")

        if fix_buildings:
            n = self._fix_builders(P, old, new)
            plan.note(f"Mark {n} structure(s) you built as built by the new ID.")

        # 4. Player files.
        plan.player_old = self.player_files[old]
        plan.player_new = self.player_files.get(new)
        plan.note(f"Players/{old}.sav -> Players/{new}.sav (IDs inside updated).")
        if plan.player_old.dps_path:
            plan.note(f"Players/{old}_dps.sav -> Players/{new}_dps.sav (Dimensional Pal Storage).")
        else:
            plan.warn("No _dps.sav (Dimensional Pal Storage) for the old character; skipping.")
        stray = [c for c in self.characters if c.is_player and c.uid == old and c is not old_char]
        if stray:
            plan.warn("More than one player character uses the old ID; only the first was moved.")
        return plan

    def _remove_containers(self, P: Patcher, ids: set[str]) -> int:
        n = 0
        for mapname in ("CharacterContainerSaveData", "ItemContainerSaveData"):
            m = self.ws.get(mapname)
            if m is None:
                continue
            for e in m.children:
                k = e.get("Key")
                idn = k.get("ID") if k is not None else None
                if idn is not None and idn.value in ids:
                    P.remove_element(e)
                    n += 1
        return n

    def _remove_handle(self, P: Patcher, g: Guild, off: int):
        P.replace(off, off + 32, b"", g.raw)
        P.adjust_int(g.handles_count_off, -1)

    def _delete_guild(self, plan: "TransferPlan", g: Guild):
        gid = guid_to_bytes(g.group_id)
        bases = self.ws.get("BaseCampSaveData")
        if bases is not None and self.data.find(gid, bases.start, bases.end) >= 0:
            raise TransferError(
                "The new character has its own guild with a base. Dismantle that palbox (or leave "
                "the guild) in-game with the new character first, then run this again.")
        objs = self._guild_objects(g.group_id)
        if objs:
            raise TransferError(
                f"The new character's own guild owns {objs} placed object(s). Dismantle them in-game "
                "with the new character (or join your friends' guild first), then run this again.")
        plan.level.remove_element(g.entry)
        extra = self.ws.get("GuildExtraSaveDataMap")
        if extra is not None:
            for e in extra.children:
                if e.get("Key").value == g.group_id:
                    plan.level.remove_element(e)
        plan.note("Delete the solo guild the server created for the new character.")

    def _guild_objects(self, group_id: str) -> int:
        target = guid_to_bytes(group_id)
        mos = self.ws.get("MapObjectSaveData")
        n = 0
        for e in mos.children if mos else []:
            raw = e.path("Model", "RawData")
            if raw is None or not isinstance(raw.value, tuple):
                continue
            s, l = raw.value
            if l >= MODEL_GROUP_OFFSET + 16 and self.data[s + MODEL_GROUP_OFFSET:s + MODEL_GROUP_OFFSET + 16] == target:
                n += 1
        return n

    def _fix_builders(self, P: Patcher, old: str, new: str) -> int:
        oldb = guid_to_bytes(old)
        zero = bytes(16)
        mos = self.ws.get("MapObjectSaveData")
        n = 0
        for e in mos.children if mos else []:
            raw = e.path("Model", "RawData")
            if raw is None or not isinstance(raw.value, tuple):
                continue
            s, l = raw.value
            if l < MODEL_BUILDER_OFFSET + 16:
                continue
            # Only player-built objects (they belong to a guild); world
            # objects in co-op saves also carry ...0001 as their builder.
            if self.data[s + MODEL_GROUP_OFFSET:s + MODEL_GROUP_OFFSET + 16] == zero:
                continue
            if self.data[s + MODEL_BUILDER_OFFSET:s + MODEL_BUILDER_OFFSET + 16] == oldb:
                P.set_guid(s + MODEL_BUILDER_OFFSET, new)
                n += 1
        return n


class TransferPlan:
    def __init__(self, world: World, old: str, new: str):
        self.world = world
        self.old, self.new = old, new
        self.level = Patcher(world.data)
        self.steps: list[str] = []
        self.warnings: list[str] = []
        self.player_old: PlayerFile | None = None
        self.player_new: PlayerFile | None = None

    def note(self, s):
        self.steps.append(s)

    def warn(self, s):
        self.warnings.append(s)

    def describe(self) -> str:
        lines = ["Planned changes:"] + [f"  - {s}" for s in self.steps]
        if self.warnings:
            lines += ["", "Warnings:"] + [f"  ! {w}" for w in self.warnings]
        return "\n".join(lines)

    # -- building the new files ------------------------------------------
    def _player_sav(self, path: Path) -> bytes:
        data, _ = read_sav(path)
        _, root = parse_gvas(data)
        P = Patcher(data)
        sd = root.get("SaveData")
        hits = 0
        for n in (sd.get("PlayerUId"), sd.path("IndividualId", "PlayerUId")):
            if n is not None and n.value == self.old:
                P.set_guid(n.end - 16, self.new)
                hits += 1
        if hits == 0:
            raise TransferError(f"{path.name} does not contain the old player ID.")
        return P.apply()

    def _dps_sav(self, path: Path) -> bytes:
        data, _ = read_sav(path)
        _, root = parse_gvas(data)
        P = Patcher(data)
        arr = root.get("SaveParameterArray")
        for el in arr.children if arr else []:
            sp = el.get("SaveParameter")
            for n in sp.walk() if sp else []:
                if n.type == "StructProperty" and n.struct_type == "Guid" and n.value == self.old:
                    P.set_guid(n.end - 16, self.new)
        return P.apply()

    def build(self) -> dict[Path, bytes]:
        """Produce new uncompressed contents for every file to write and
        verify each one parses cleanly."""
        w = self.world
        out: dict[Path, bytes] = {}
        level = self.level.apply()
        _, root = parse_gvas(level, LEVEL_SKIP)
        if root.get("worldSaveData") is None:
            raise TransferError("Internal check failed: patched Level.sav did not parse.")
        out[w.level_path] = level
        out[w.players_dir / f"{self.new}.sav"] = self._player_sav(self.player_old.path)
        if self.player_old.dps_path:
            out[w.players_dir / f"{self.new}_dps.sav"] = self._dps_sav(self.player_old.dps_path)
        return out

    def verify(self, level: bytes) -> list[str]:
        """Re-read the patched Level.sav and confirm the result."""
        problems = []
        w2 = World.__new__(World)
        w2.folder, w2.log = self.world.folder, (lambda *_: None)
        w2.data = level
        w2.header, w2.root = parse_gvas(level, LEVEL_SKIP)
        w2.ws = w2.root.get("worldSaveData")
        w2.characters, w2.guilds = [], []
        w2.player_files = {**self.world.player_files, self.new: None}
        w2._load_characters()
        w2._load_guilds()
        if len(structure_check(level)) > len(structure_check(self.world.data)):
            problems.append("the edited Level.sav no longer parses completely")
        if len(w2.characters) != len(w2.ws.get("CharacterSaveParameterMap").children):
            problems.append("a character entry failed to parse")
        mine = [c for c in w2.characters if c.is_player and c.uid == self.new]
        if len(mine) != 1:
            problems.append(f"expected exactly one player with the new ID, found {len(mine)}")
        if w2.player_character(self.old) is not None:
            problems.append("the old ID still has a player character")
        old_char = self.world.player_character(self.old)
        if mine and old_char and mine[0].instance_id != old_char.instance_id:
            problems.append("the character under the new ID is not your old character")
        if any(c.owner == self.old for c in w2.characters):
            problems.append("some pals are still owned by the old ID")
        g = w2.guild_of(self.new)
        if self.world.guild_of(self.old) is not None and g is None:
            problems.append("you are no longer in your guild")
        before = {g.group_id: len(g.warnings) for g in self.world.guilds}
        for g in w2.guilds:
            if len(g.warnings) > before.get(g.group_id, 0):
                problems.append(f"guild '{g.name}' no longer parses cleanly: {g.warnings}")
        return problems

    def apply(self, backup=True, log=print) -> Path | None:
        w = self.world
        files = self.build()
        problems = self.verify(files[w.level_path])
        if problems:
            raise TransferError("Safety check failed, nothing was written:\n  " + "\n  ".join(problems))
        bk = make_backup(w.folder, log) if backup else None
        for path, raw in files.items():
            log(f"Writing {path.relative_to(w.folder)}")
            write_sav(path, raw)
        for p in (self.player_old.path, self.player_old.dps_path):
            if p and p.exists() and p not in files:
                log(f"Removing {p.relative_to(w.folder)} (now saved under the new ID)")
                p.unlink()
        if self.player_new and self.player_new.dps_path and self.player_old.dps_path is None:
            log(f"Removing {self.player_new.dps_path.name} (belonged to the throw-away character)")
            self.player_new.dps_path.unlink()
        return bk


def make_backup(folder: Path, log=print) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = folder.parent / f"{folder.name}_backup_{stamp}.zip"
    log(f"Backing up world to {dest} ...")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in folder.rglob("*"):
            if p.is_file():
                z.write(p, Path(folder.name) / p.relative_to(folder))
    return dest


def restore_backup(zip_path: Path, folder: Path, log=print):
    """Replace ``folder`` with the contents of a backup made by make_backup."""
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        prefix = folder.name + "/"
        if not names or not all(n.replace("\\", "/").startswith(prefix) for n in names):
            raise TransferError(f"{zip_path.name} is not a backup of {folder.name}")
        safety = make_backup(folder, log)
        log(f"(current state saved to {safety.name})")
        for p in folder.joinpath("Players").glob("*.sav") if (folder / "Players").is_dir() else []:
            p.unlink()
        z.extractall(folder.parent)
    log(f"Restored {folder} from {zip_path.name}")


def copy_local_data(src_world: Path, dst_world: Path, log=print) -> Path:
    """Copy LocalData.sav (your personal map exploration / fog of war)."""
    src = Path(src_world) / "LocalData.sav"
    if not src.is_file():
        raise TransferError(f"No LocalData.sav in {src_world}")
    dst = Path(dst_world) / "LocalData.sav"
    if dst.exists():
        bak = dst.with_name(f"LocalData.sav.bak_{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(dst, bak)
        log(f"Existing LocalData.sav saved as {bak.name}")
    Path(dst_world).mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    log(f"Copied map exploration to {dst}")
    return dst


def scan_uid(world: World, uid: str) -> list[tuple[str, int]]:
    """Diagnostics: every byte-level occurrence of ``uid`` in Level.sav,
    labelled with the innermost parsed property path."""
    import collections
    pat = guid_to_bytes(norm_uid(uid))
    counts = collections.Counter()
    i = world.data.find(pat)
    while i >= 0:
        n = world.root
        while True:
            nxt = next((c for c in n.children if c.start <= i < c.end), None)
            if nxt is None:
                break
            n = nxt
        path = ".".join("#" if p.isdigit() else p for p in n.full_path().split("."))
        counts[path or "(header)"] += 1
        i = world.data.find(pat, i + 1)
    return counts.most_common()
