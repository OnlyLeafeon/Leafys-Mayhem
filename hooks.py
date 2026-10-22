from __future__ import annotations

import argparse

from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from mods_base import ENGINE, command, get_pc, hook

from .locks import ensure_default_weapon_locks
from .utils import log, send_location_check


def _read_mission_list():
    pc = get_pc()
    playthrough_index = ENGINE.GameViewport.World.GameState.CurrentPlayThrough - 1
    return pc.PlayerMissionComponent.MissionPlaythroughs[playthrough_index].MissionList


def _mission_internal_name(mission_class_str: str) -> str:
    bare = mission_class_str.split("'", 1)[1].rstrip("'") if "'" in mission_class_str else mission_class_str
    stem = bare.rsplit("/", 1)[-1].split(".")[0]
    return stem


@command(description="Print every mission's current status right now, bypassing the tick-based tracking below")
def ap_dump_missions(args: argparse.Namespace) -> None:
    try:
        mission_list = _read_mission_list()
    except Exception as e:
        log(f"<mission dump> couldn't read MissionList: {e!r}")
        return
    for mission in mission_list:
        try:
            key = str(mission.MissionClass)
            status = int(mission.Status)
        except Exception as e:
            log(f"<mission dump> couldn't read a mission's fields: {e!r}")
            continue
        log(f"<mission dump> {key} = {status}")


_logged_kill_name_fallback = False
_logged_unique_check_fallback = False


def _dump_name_like_properties(obj: UObject) -> None:
    log(f"<kill debug> scanning {obj!r} for name-like properties ...")
    try:
        for prop in obj.Class._properties():
            pname = prop.Name
            if not any(word in pname.lower() for word in ("name", "title", "display")):
                continue
            try:
                value = obj._get_field(prop)
            except Exception as e:
                value = f"<unreadable: {e!r}>"
            log(f"<kill debug>   {pname} = {value!r}")
    except Exception as e:
        log(f"<kill debug> property scan failed: {e!r}")


def _get_display_name(pawn: UObject) -> str:
    playthrough_index = ENGINE.GameViewport.World.GameState.CurrentPlayThrough - 1
    balance_state = pawn.AIBalanceState.PlayThroughs[playthrough_index]

    if bool(balance_state.bOverrideUIDisplayName):
        ui_name_asset = balance_state.DisplayUIName
        if ui_name_asset is not None:
            return str(ui_name_asset.DisplayName)

    return str(balance_state.DisplayName)


def _is_unique_pawn(pawn: UObject) -> bool:
    global _logged_unique_check_fallback
    try:
        class_path = str(pawn.Class)
    except Exception as e:
        if not _logged_unique_check_fallback:
            _logged_unique_check_fallback = True
            log(f"<kill debug> couldn't read pawn.Class to check uniqueness: {e!r}")
        return False
    return "/_unique/" in class_path.lower()


@hook("/Script/GbxGameSystemCore.DamageComponent:ReceiveHealthDepleted", Type.PRE)
def _on_health_depleted(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    global _logged_kill_name_fallback

    try:
        owner = obj.GetOwner()
    except Exception:
        owner = None

    if owner is None:
        log("<kill> <no owner>")
        return

    try:
        name = _get_display_name(owner)
    except Exception as e:
        log(f"<kill> {owner!r} (display name lookup failed: {e!r})")
        if not _logged_kill_name_fallback:
            _logged_kill_name_fallback = True
            _dump_name_like_properties(owner)
        return

    log(f"<kill> {name}")

    if _is_unique_pawn(owner):
        send_location_check(f"Kill: {name}")


_last_mission_status: dict[str, int] = {}


@hook("/Script/Engine.Actor:ReceiveTick", Type.POST)
def _on_actor_tick(obj: UObject, args: WrappedStruct, ret, func: BoundFunction) -> None:
    if "BPCont_Player_C_0" not in str(obj):
        return
    ensure_default_weapon_locks()

    try:
        mission_list = _read_mission_list()
    except Exception:
        return

    for mission in mission_list:
        try:
            key = str(mission.MissionClass)
            status = int(mission.Status)
        except Exception:
            continue
        if _last_mission_status.get(key) != status:
            _last_mission_status[key] = status
            log(f"<mission status> {key} -> {status}")
            if status == 2:
                send_location_check(_mission_internal_name(key))
