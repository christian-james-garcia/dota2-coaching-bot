"""OpenDota game constants: hero, ability, item and patch data.

Nine module-level maps had one writer and six readers, always read together,
and every reader therefore depended on import-time global state. Holding them
on one object makes those readers take their data as an argument, which is what
makes them testable.
"""
import time

import aiohttp

from config import (
    GAME_DATA_REFRESH_SECONDS,
    GAME_DATA_RETRY_SECONDS,
    TRANSIENT_ERRORS,
)
from reporting import report_unexpected


class GameData:
    """Current-patch reference data, refreshed from OpenDota on an interval."""

    def __init__(self):
        self.hero_names = {}
        self.hero_unit_names = {}
        self.hero_abilities = {}
        self.hero_ability_details = {}
        self.ability_names = {}
        self.item_names = {}
        self.item_key_names = {}
        self.item_details = {}
        self.patch_names = {}
        self.current_patch_id = None
        self.current_patch_name = None
        self.last_refresh = 0.0
        self.last_attempt = 0.0

    def __bool__(self):
        """True once hero data is loaded, so callers can check readiness."""
        return bool(self.hero_names)

    def hero_name(self, hero_id):
        """Hero name for an id, falling back to a visible placeholder."""
        return self.hero_names.get(hero_id, f"Hero_{hero_id}")

    def patch_label(self, patch_id):
        if patch_id is None:
            return "unavailable"
        return self.patch_names.get(int(patch_id), f"OpenDota patch id {patch_id}")

    # A compact set: the whole catalog would crowd the prompt without adding
    # anything the coaching sections ask about.
    COACHING_ITEM_KEYS = (
        "black_king_bar", "linkens_sphere", "lotus_orb", "ghost", "glimmer_cape",
        "force_staff", "hurricane_pike", "aeon_disk", "pipe", "crimson_guard",
        "heavens_halberd", "orchid", "bloodthorn", "scythe_of_vyse", "shivas_guard",
        "nullifier", "spirit_vessel", "rod_of_atos", "cyclone", "manta",
    )

    def item_catalog(self) -> dict:
        """Return current patch descriptions for a compact set of coaching items."""
        catalog = {}
        for key in self.COACHING_ITEM_KEYS:
            details = self.item_details.get(key) or {}
            description = " ".join((details.get("description") or "").split())
            if details.get("name") and description:
                catalog[details["name"]] = description[:450]
        return catalog

    async def refresh(self, force: bool = False):
        """Refresh OpenDota's game constants so reports use current patch data."""
        now = time.time()
        if not force and now - self.last_refresh < GAME_DATA_REFRESH_SECONDS:
            return
        if not force and now - self.last_attempt < GAME_DATA_RETRY_SECONDS:
            return
        self.last_attempt = now

        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            refreshed_everything = True

            try:
                async with session.get("https://api.opendota.com/api/constants/patch") as resp:
                    if resp.status == 200:
                        patches = await resp.json()
                        self.patch_names = {int(p["id"]): p["name"] for p in patches if p.get("id") is not None}
                        if self.patch_names:
                            self.current_patch_id = max(self.patch_names)
                            self.current_patch_name = self.patch_names[self.current_patch_id]
                            print(f"[PATCH] Current OpenDota game-data patch: {self.current_patch_name} (id {self.current_patch_id}).", flush=True)
                        else:
                            refreshed_everything = False
                    else:
                        refreshed_everything = False
                        print(f"[PATCH] Failed to fetch patch constants. Status: {resp.status}", flush=True)
            except TRANSIENT_ERRORS as exc:
                refreshed_everything = False
                print(f"[PATCH] Could not load patch constants: {exc}", flush=True)
            except Exception as exc:
                refreshed_everything = False
                report_unexpected("parsing patch constants", exc)

            try:
                async with session.get("https://api.opendota.com/api/heroes") as resp:
                    if resp.status == 200:
                        heroes = await resp.json()
                        self.hero_names = {h["id"]: h["localized_name"] for h in heroes}
                        self.hero_unit_names = {h["id"]: h["name"] for h in heroes}
                        print(f"[HEROES] Loaded {len(self.hero_names)} hero constants.", flush=True)
                    else:
                        refreshed_everything = False
                        print(f"[HEROES] Failed to fetch hero constants. Status: {resp.status}", flush=True)
            except TRANSIENT_ERRORS as exc:
                refreshed_everything = False
                print(f"[HEROES] Could not load hero constants: {exc}", flush=True)
            except Exception as exc:
                refreshed_everything = False
                report_unexpected("parsing hero constants", exc)

            try:
                async with session.get("https://api.opendota.com/api/constants/items") as resp:
                    if resp.status == 200:
                        items = await resp.json()
                        self.item_names = {
                            int(item["id"]): item.get("dname", key.replace("_", " ").title())
                            for key, item in items.items()
                            if item.get("id") is not None
                        }
                        self.item_key_names = {
                            key: item.get("dname", key.replace("_", " ").title())
                            for key, item in items.items()
                        }
                        self.item_details = {
                            key: {
                                "name": item.get("dname", key.replace("_", " ").title()),
                                "description": item.get("desc") or " ".join(
                                    ability.get("description", "") for ability in item.get("abilities", [])
                                    if ability.get("description")
                                ),
                            }
                            for key, item in items.items()
                        }
                        print(f"[ITEMS] Loaded {len(self.item_names)} item names.", flush=True)
                    else:
                        refreshed_everything = False
                        print(f"[ITEMS] Failed to fetch item constants. Status: {resp.status}", flush=True)
            except TRANSIENT_ERRORS as exc:
                refreshed_everything = False
                print(f"[ITEMS] Could not load item constants: {exc}", flush=True)
            except Exception as exc:
                refreshed_everything = False
                report_unexpected("parsing item constants", exc)

            try:
                async with session.get("https://api.opendota.com/api/constants/hero_abilities") as hero_resp:
                    async with session.get("https://api.opendota.com/api/constants/abilities") as ability_resp:
                        if hero_resp.status == 200 and ability_resp.status == 200:
                            hero_abilities = await hero_resp.json()
                            abilities = await ability_resp.json()
                            self.ability_names = {
                                ability_key: ability.get("dname", ability_key)
                                for ability_key, ability in abilities.items()
                                if ability.get("dname")
                            }
                            ability_map = {}
                            ability_details_map = {}
                            for hero_id, unit_name in self.hero_unit_names.items():
                                raw_names = hero_abilities.get(unit_name, {}).get("abilities", [])
                                # OpenDota nests a list here for heroes with a second
                                # form (Monkey King). An unflattened list is unhashable,
                                # so the `name in abilities` test below used to raise and
                                # abort the whole loop, losing ability data for every hero.
                                names = []
                                for entry in raw_names:
                                    if isinstance(entry, list):
                                        names.extend(entry)
                                    else:
                                        names.append(entry)
                                names = [name for name in names if isinstance(name, str)]
                                ability_map[self.hero_names[hero_id]] = [
                                    abilities[name].get("dname", name)
                                    for name in names
                                    if name in abilities and abilities[name].get("dname")
                                ]
                                ability_details_map[self.hero_names[hero_id]] = [
                                    {
                                        "name": abilities[name].get("dname", name),
                                        "description": abilities[name].get("desc", ""),
                                    }
                                    for name in names
                                    if name in abilities and abilities[name].get("dname")
                                ]
                            self.hero_abilities = ability_map
                            self.hero_ability_details = ability_details_map
                            print(f"[ABILITIES] Loaded current abilities for {len(self.hero_abilities)} heroes.", flush=True)
                        else:
                            refreshed_everything = False
                            print(
                                f"[ABILITIES] Could not load ability constants: "
                                f"hero HTTP {hero_resp.status}, abilities HTTP {ability_resp.status}",
                                flush=True,
                            )
            except TRANSIENT_ERRORS as exc:
                refreshed_everything = False
                print(f"[ABILITIES] Could not load ability constants: {exc}", flush=True)
            except Exception as exc:
                refreshed_everything = False
                report_unexpected("parsing ability constants", exc)

            if refreshed_everything:
                self.last_refresh = time.time()
                print(f"[CONSTANTS] Current hero, item, ability, and patch data refreshed.", flush=True)
