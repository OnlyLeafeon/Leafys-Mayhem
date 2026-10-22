from __future__ import annotations

import argparse
import re

import unrealsdk
from unrealsdk.hooks import Block, Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from mods_base import command, hook

from .utils import log

ALL_TIERS = ("Common", "Uncommon", "Rare", "VeryRare", "Legendary")

_locked: set[str] = {"Uncommon", "Rare", "VeryRare", "Legendary"}

_LOCAL_RARITY_RE = re.compile(r"Att_LocalRarityModifier_\d+_([A-Za-z]+)")
_seen_attributes: set[str] = set()


def unlock_rarity(tier: str) -> None:
    _locked.discard(tier)
    log(f"<rarity> unlocked {tier}")


def lock_rarity(tier: str) -> None:
    _locked.add(tier)
    log(f"<rarity> locked {tier}")

WEAPON_TYPE_FOLDERS = ("Pistols", "Shotguns", "SMGs", "SniperRifles", "AssaultRifles", "HeavyWeapons")

_GUNS_POOL_BASE = "/Game/GameData/Loot/ItemPools/Guns"
RARITY_POOLS: dict[str, str] = {
    tier: f"{_GUNS_POOL_BASE}/ItemPool_Guns_{tier}.ItemPool_Guns_{tier}" for tier in ALL_TIERS
}

WEAPON_TYPE_SUBPOOLS: dict[str, dict[str, str]] = {
    "Pistols": {
        tier: f"{_GUNS_POOL_BASE}/Pistols/ItemPool_Pistols_{tier}.ItemPool_Pistols_{tier}" for tier in ALL_TIERS
    },
    "Shotguns": {
        tier: f"{_GUNS_POOL_BASE}/Shotguns/ItemPool_Shotguns_{tier}.ItemPool_Shotguns_{tier}" for tier in ALL_TIERS
    },
    "SMGs": {
        tier: f"{_GUNS_POOL_BASE}/SMG/ItemPool_SMGs_{tier}.ItemPool_SMGs_{tier}" for tier in ALL_TIERS
    },
    "SniperRifles": {
        tier: (
            f"{_GUNS_POOL_BASE}/SniperRifles/ItemPool_SnipeRifles_{tier}.ItemPool_SnipeRifles_{tier}"
            if tier in ("VeryRare", "Legendary")
            else f"{_GUNS_POOL_BASE}/SniperRifles/ItemPool_SniperRifles_{tier}.ItemPool_SniperRifles_{tier}"
        )
        for tier in ALL_TIERS
    },
    "AssaultRifles": {
        tier: f"{_GUNS_POOL_BASE}/AssaultRifles/ItemPool_AssaultRifles_{tier}.ItemPool_AssaultRifles_{tier}"
        for tier in ALL_TIERS
    },
    "HeavyWeapons": {
        tier: f"{_GUNS_POOL_BASE}/Heavy/ItemPool_Heavy_{tier}.ItemPool_Heavy_{tier}" for tier in ALL_TIERS
    },
}

_locked_weapon_types: set[str] = set(WEAPON_TYPE_FOLDERS)  # all locked by default, same stance as rarity

_subpool_entry_cache: dict[tuple[str, str], tuple[object, float, float]] = {}


def _balanced_item_pool_name(entry) -> str | None:
    pool_ref = entry.ItemPoolData
    if pool_ref is None:
        return None
    text = str(pool_ref)
    if "'" in text:
        text = text.split("'", 1)[1].rstrip("'")
    return text.rsplit(".", 1)[-1]


def _set_subpool_entry_locked(parent_pool_path: str, subpool_path: str, locked: bool) -> None:
    parent = unrealsdk.find_object("ItemPoolData", parent_pool_path)
    if parent is None:
        log(f"<weapon type> parent pool not found: {parent_pool_path}")
        return
    log(f"<weapon type debug> asked for {parent_pool_path}, find_object gave {parent!r}")

    target_name = subpool_path.rsplit(".", 1)[-1]
    key = (parent_pool_path, subpool_path)
    matched = False

    for entry in parent.BalancedItems:
        if _balanced_item_pool_name(entry) != target_name:
            continue
        matched = True
        if locked:
            if key not in _subpool_entry_cache:
                _subpool_entry_cache[key] = (
                    entry.Weight.BaseValueAttribute,
                    entry.Weight.BaseValueConstant,
                    entry.Weight.BaseValueScale,
                )
            entry.Weight.BaseValueAttribute = None
            entry.Weight.BaseValueConstant = 0
            entry.Weight.BaseValueScale = 0
            log(f"<weapon type> zeroed {target_name} in {parent_pool_path}")
        else:
            cached = _subpool_entry_cache.get(key)
            if cached is not None:
                entry.Weight.BaseValueAttribute, entry.Weight.BaseValueConstant, entry.Weight.BaseValueScale = cached
            log(f"<weapon type> restored {target_name} in {parent_pool_path}")
        break

    if not matched:
        found = [n for n in (_balanced_item_pool_name(e) for e in parent.BalancedItems) if n]
        log(f"<weapon type> entry for {target_name} not found in {parent_pool_path} - entries present: {found}")


def weapon_type_from_path(path: str) -> str | None:
    for folder in WEAPON_TYPE_FOLDERS:
        if f"/{folder}/" in path:
            return folder
    return None


def unlock_weapon_type(weapon_type: str) -> None:
    _locked_weapon_types.discard(weapon_type)
    for tier, subpool_path in WEAPON_TYPE_SUBPOOLS.get(weapon_type, {}).items():
        _set_subpool_entry_locked(RARITY_POOLS[tier], subpool_path, False)
    log(f"<weapon type> unlocked {weapon_type}")


def lock_weapon_type(weapon_type: str) -> None:
    _locked_weapon_types.add(weapon_type)
    for tier, subpool_path in WEAPON_TYPE_SUBPOOLS.get(weapon_type, {}).items():
        _set_subpool_entry_locked(RARITY_POOLS[tier], subpool_path, True)
    log(f"<weapon type> locked {weapon_type}")

_default_locks_applied = False


def ensure_default_weapon_locks() -> None:
    global _default_locks_applied
    if _default_locks_applied:
        return
    for subpools in WEAPON_TYPE_SUBPOOLS.values():
        for subpool_path in subpools.values():
            if unrealsdk.find_object("ItemPoolData", subpool_path) is None:
                return
    for weapon_type in WEAPON_TYPE_FOLDERS:
        lock_weapon_type(weapon_type)
    _default_locks_applied = True
    log("<weapon type> default locks applied (all types, all tiers)")


@hook("/Script/GbxGameSystemCore.GbxAttributeFunctionLibrary:GetValueOfAttribute", Type.PRE)
def _on_get_value_of_attribute(
    obj: UObject,
    args: WrappedStruct,
    ret,
    func: BoundFunction,
) -> tuple[type[Block], float] | None:
    attribute = str(args.Attribute)

    if "LocalRarityModifier" in attribute:
        match = _LOCAL_RARITY_RE.search(attribute)
        if match is not None:
            tier = match.group(1)
            if attribute not in _seen_attributes:
                _seen_attributes.add(attribute)
                log(f"<rarity> saw attribute {attribute} (tier {tier}, {'LOCKED' if tier in _locked else 'open'})")
            if tier in _locked:
                return Block, 0.0
        return None

    return None


def _normalize(tier: str) -> str | None:
    for known in ALL_TIERS:
        if known.lower() == tier.lower():
            return known
    return None


def _normalize_weapon_type(weapon_type: str) -> str | None:
    for known in WEAPON_TYPE_FOLDERS:
        if known.lower() == weapon_type.lower():
            return known
    return None


@command(description="Unlock a rarity's drops for testing (Common/Uncommon/Rare/VeryRare/Legendary)")
def ap_unlock_rarity(args: argparse.Namespace) -> None:
    tier = _normalize(args.tier)
    if tier is None:
        log(f"<rarity> unknown tier {args.tier!r} - use one of {', '.join(ALL_TIERS)}")
        return
    unlock_rarity(tier)


ap_unlock_rarity.add_argument("tier", help="Common, Uncommon, Rare, VeryRare or Legendary")


@command(description="Lock a rarity's drops for testing (Common/Uncommon/Rare/VeryRare/Legendary)")
def ap_lock_rarity(args: argparse.Namespace) -> None:
    tier = _normalize(args.tier)
    if tier is None:
        log(f"<rarity> unknown tier {args.tier!r} - use one of {', '.join(ALL_TIERS)}")
        return
    lock_rarity(tier)


ap_lock_rarity.add_argument("tier", help="Common, Uncommon, Rare, VeryRare or Legendary")


@command(description="Show which rarities are currently locked")
def ap_rarities(args: argparse.Namespace) -> None:  # noqa: ARG001 - required by command signature
    for tier in ALL_TIERS:
        log(f"<rarity> {tier}: {'LOCKED' if tier in _locked else 'open'}")


@command(description="Unlock a weapon type's drops for testing (Pistols/Shotguns/SMGs/SniperRifles/AssaultRifles/HeavyWeapons)")
def ap_unlock_type(args: argparse.Namespace) -> None:
    weapon_type = _normalize_weapon_type(args.weapon_type)
    if weapon_type is None:
        log(f"<weapon type> unknown type {args.weapon_type!r} - use one of {', '.join(WEAPON_TYPE_FOLDERS)}")
        return
    unlock_weapon_type(weapon_type)


ap_unlock_type.add_argument("weapon_type", help="Pistols, Shotguns, SMGs, SniperRifles, AssaultRifles or HeavyWeapons")


@command(description="Lock a weapon type's drops for testing (Pistols/Shotguns/SMGs/SniperRifles/AssaultRifles/HeavyWeapons)")
def ap_lock_type(args: argparse.Namespace) -> None:
    weapon_type = _normalize_weapon_type(args.weapon_type)
    if weapon_type is None:
        log(f"<weapon type> unknown type {args.weapon_type!r} - use one of {', '.join(WEAPON_TYPE_FOLDERS)}")
        return
    lock_weapon_type(weapon_type)


ap_lock_type.add_argument("weapon_type", help="Pistols, Shotguns, SMGs, SniperRifles, AssaultRifles or HeavyWeapons")


@command(description="Show which weapon types are currently locked")
def ap_types(args: argparse.Namespace) -> None:  # noqa: ARG001 - required by command signature
    for weapon_type in WEAPON_TYPE_FOLDERS:
        log(f"<weapon type> {weapon_type}: {'LOCKED' if weapon_type in _locked_weapon_types else 'open'}")


