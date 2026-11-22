"""Mech bot: farm cubes early, strafe in fights, retreat to heal, lead every shot.

How a decision is made (runs 10 times per second, in `act`):
  1. Bookkeeping: answer upgrade offers, remember hits, decide whether we're retreating.
  2. Default plan: walk toward the next zone circle.
  3. If an enemy mech is visible -> `fight` (shoot it + strafe/retreat).
     Otherwise                   -> `explore` (reload, farm crates/drones, collect loot, heal).
  4. Last-minute fixes that apply in any situation: get unstuck, pick up weapons underfoot.
Each step only overwrites the parts of the action it cares about, so later steps win.
"""
import math
import random
import mechfight as mf

# ---------------------------------------------------------------------- tuning knobs
# Everything you might want to tweak lives here, so you don't have to dig through the logic.

# Upgrade ids in order of preference. Anything not listed ranks last.
UPGRADE_PRIORITY = ["armor", "plating", "gun_damage", "nanites", "gun_rate", "gyros",
                    "laser_damage", "laser_charge", "lightning_efficiency", "lightning_range",
                    "gun_mag", "servos"]
WANTED_WEAPONS = ("laser", "lightning")  # we never fire the mortar, so don't carry one

RETREAT_BELOW = 0.40  # start backing off under this fraction of max HP
RETREAT_UNTIL = 0.85  # fight again once healed above this fraction
# Two different thresholds on purpose ("hysteresis"): with a single threshold, HP hovering
# around it would make the bot switch between fighting and fleeing every tick.

FARM_RANGE = 70       # shoot crates/drones this close (so their cubes land within LOOT_RANGE)
LOOT_RANGE = 80       # walk to cubes and wanted weapons this close
REACH_Z = 4           # cubes/weapons more than this far above/below us are out of reach
DROP_RANGE = 0        # go for supply drops this close (we can walk ~290 m before one lands)

# Positions in observations are the target's FEET. We aim at its middle instead:
# half of each target's height (mech 9 m, drone 3.5 m, crate 2.4 m).
CENTER_HEIGHT = {"mech": 4.5, "npc": 1.75, "crate": 1.2}


# ---------------------------------------------------------------------- small math helpers
def angle_diff(a, b):
    """Shortest signed turn from angle b to angle a, in radians (-pi to pi)."""
    # Same formula you wrote: shift by pi, wrap with %, shift back.
    return (a - b + math.pi) % (2 * math.pi) - math.pi


def heading(yaw, speed=1.0):
    """Move vector that walks toward `yaw` at a fraction of full speed."""
    # cos/sin turn an angle back into an (east, north) direction of length 1.
    # Multiplying by speed < 1 makes the vector shorter, and the game walks slower.
    return [speed * math.cos(yaw), speed * math.sin(yaw)]


def yaw_to(src, dst):
    """Compass yaw from point src to point dst (x/y only)."""
    # The same atan2(dy, dx) the example bot used, wrapped so we can reuse it everywhere.
    return math.atan2(dst[1] - src[1], dst[0] - src[0])


def in_zone(pos, zone):
    """True if a point is inside the current zone circle."""
    # A point is inside a circle if its distance to the center is less than the radius.
    return math.dist(pos[:2], zone["center"]) < zone["radius"]


class Bot:
    name = "jgnaw-mech"

    def __init__(self, hello: mf.Hello):
        # Seeding the random generator with our seed makes the "random" strafe timing the
        # same every time we replay the same match, which makes testing repeatable.
        random.seed(hello["seed"])
        self.reach = hello["config"]["loot"]["pickup_radius"]

        # Everything stored on `self` survives between ticks (act() runs fresh each time).
        self.threat_bearing = None   # direction of the last hit from someone we couldn't see
        self.threat_time = -100.0    # when it happened (-100 = "long ago")
        self.strafe_side = 1         # +1 = strafe to the enemy's left, -1 = to its right
        self.next_flip = 0.0         # match time at which we switch strafe side
        self.last_flip = 0.0         # match time we last switched (to avoid switching too fast)
        self.retreating = False      # True while HP is low and we're backing off to heal
        self.stuck_since = None      # match time we started pushing without moving
        self.jet_until = -1.0        # keep jetting upward until this time after a stuck-jump

    # ------------------------------------------------------------------ main loop
    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        zone = obs["zone"]
        act: mf.Action = {"tick": obs["tick"]}

        # 1. Bookkeeping.
        if obs["offer"]:
            act["pick"] = self.pick_upgrade(obs["offer"])
        self.remember_threats(obs)
        self.update_retreat(me)

        # 2. Default: walk to the next zone circle, sprinting if we're outside the zone.
        zone_yaw = yaw_to(me["pos"], zone["next_center"])
        act.update(move=heading(zone_yaw), aim=[zone_yaw, 0.0], boost=not me["in_zone"])

        # 3. Fight the nearest visible mech, or do peaceful things if none is visible.
        enemies = sorted((m for m in obs["mechs"] if m["kind"] == "mech"),
                         key=lambda m: math.dist(m["pos"], me["pos"]))
        if enemies:
            # Weakest-in-100-m targeting (scored 2.43-2.5 vs 2.7 for nearest), kept for a
            # possible "sticky target" version: re-picking every tick likely made the torso
            # flip between enemies, so it stopped firing while turning.
            # weakest_e = enemies[0]
            # for e in enemies:
            #     if math.dist(e["pos"], me["pos"]) < 100.0 and e.get("hp", 9999) < weakest_e["hp"]:
            #         weakest_e = e
            self.fight(obs, me, enemies[0], act)
        else:
            self.explore(obs, me, act)

        # 4. Fixes that apply no matter what we decided above.
        self.unstick(obs, me, act)
        self.take_weapon(obs, me, act)

        # Debug label (only for tools; it never affects the game).
        mf.draw.label(f"hp {me['hp']:.0f} cubes {me['cubes']}"
                      f"{' RETREAT' if self.retreating else ''}")
        return act

    # ------------------------------------------------------------------ decisions
    def pick_upgrade(self, offer):
        """Index of the offered upgrade that ranks best in UPGRADE_PRIORITY (0 if none listed)."""
        # Your "find the minimum" loop: rank = position in OUR list (lower = better),
        # index = position in THE OFFER (what we send back).
        best_index, best_rank = 0, len(UPGRADE_PRIORITY)
        for i, choice in enumerate(offer["choices"]):
            if choice["id"] in UPGRADE_PRIORITY:
                rank = UPGRADE_PRIORITY.index(choice["id"])
                if rank < best_rank:
                    best_index, best_rank = i, rank
        return best_index

    def remember_threats(self, obs):
        """Store the direction and time of the latest hit that has a direction (not zone)."""
        for event in obs["events"]:
            if event["type"] == "damaged" and event["bearing"] is not None:
                self.threat_bearing = event["bearing"]
                self.threat_time = obs["t"]

    def update_retreat(self, me):
        """Retreat when HP is low; stay retreating until mostly healed (avoids flip-flopping)."""
        frac = me["hp"] / me["max_hp"]  # use max_hp, since cubes and upgrades raise it
        if frac < RETREAT_BELOW:
            self.retreating = True
        elif frac > RETREAT_UNTIL:
            self.retreating = False
        # Between the two thresholds we keep whatever we were doing.

    def fight(self, obs, me, enemy, act):
        """Shoot the enemy, and strafe (or back off) so its leading shots miss."""
        # --- Shooting: your leading + lined-up logic, now in helper functions.
        aim, dist = self.lead(me, enemy, "mech")
        # Fire only if our ACTUAL torso yaw is within the target's half-width (about 2 m)
        # of where we want to shoot. atan2(2, dist) turns "2 m at this distance" into an angle.
        lined_up = abs(angle_diff(aim[0], me["aim"][0])) < math.atan2(2.0, dist)
        act.update(aim=aim, fire=[lined_up and dist < me["weapons"][0]["stats"]["range"],
                                  self.fire_slot1(me, dist, lined_up)])
        mf.draw.line([*me["pos"][:2], me["pos"][2] + 7],
                     [*enemy["pos"][:2], enemy["pos"][2] + 4.5], "#ff5040")

        # --- Moving.
        if not me["in_zone"]:
            # Zone damage beats everything: keep the default move (run to the zone, boosting)
            # and just shoot on the way. Legs and torso are independent, so this works.
            return

        now = obs["t"]
        speed = math.hypot(me["vel"][0], me["vel"][1])  # our horizontal speed in m/s
        # Switch strafe side at a random time 0.5-1.5 s apart, so enemies can't predict us.
        # Also switch early if we've almost stopped (we probably hit a wall) - but not within
        # 0.6 s of the last switch, because slowing down to reverse ALSO makes speed near 0
        # and we'd otherwise flip back and forth forever.
        if now >= self.next_flip or (speed < 2 and now - self.last_flip > 0.6):
            self.strafe_side = -self.strafe_side
            self.last_flip = now
            self.next_flip = now + random.uniform(0.5, 1.5)

        enemy_yaw = yaw_to(me["pos"], enemy["pos"])  # direction from us to the enemy
        if self.retreating:
            # enemy_yaw + pi = directly AWAY from the enemy. Adding +-0.6 rad (about 35 deg)
            # angles it to the current strafe side, so we still dodge while backing off.
            move_yaw = enemy_yaw + math.pi + self.strafe_side * 0.6
        else:
            # pi/2 = 90 deg = straight sideways. The extra "lean" tilts that slightly:
            #   +0.3 -> a bit more than 90 deg -> drift AWAY (good for long-range gun/laser)
            #   -0.3 -> a bit less than 90 deg -> drift CLOSER (lightning only reaches 45 m)
            slot1 = me["weapons"][1]
            lean = -0.3 if slot1 and slot1["kind"] == "lightning" else 0.3
            move_yaw = enemy_yaw + self.strafe_side * (math.pi / 2 + lean)

        # Look 15 m ahead in the strafe direction. If that point is outside the zone,
        # strafing would carry us out, so head toward the zone center instead.
        step = me["pos"][0] + 15 * math.cos(move_yaw), me["pos"][1] + 15 * math.sin(move_yaw)
        if not in_zone(step, obs["zone"]):
            move_yaw = yaw_to(me["pos"], obs["zone"]["next_center"])
        act["move"] = heading(move_yaw)

        # Boost = sprint. It costs energy, so only spend it when a full-power laser
        # (315 damage) is about to come our way. Keep 20 energy for lightning/jets.
        act["boost"] = self.laser_aimed_at_me(obs, me) and me["energy"] > 20

    def explore(self, obs, me, act):
        """No enemy in sight: reload, heal, collect loot, shoot crates and drones, take supply drops."""
        # Reload during quiet moments so the next fight starts with a full magazine.
        gun = me["weapons"][0]
        if gun["kind"] == "gun" and gun["ammo"] < gun["stats"]["mag"] and gun["reload"] == 0:
            act["reload"] = True

        # Farm: shoot the nearest crate or drone to make cubes appear. Not while retreating:
        # regen only starts after 5 s of neither TAKING nor DEALING damage, and hitting a
        # crate counts as dealing damage.
        target = None if self.retreating else self.farm_target(obs, me)
        if target:
            aim, dist = self.lead(me, target, target["kind"])
            # Tighter tolerance (1 m) than for mechs (2 m): crates and drones are small.
            lined_up = abs(angle_diff(aim[0], me["aim"][0])) < math.atan2(1.0, dist)
            act.update(aim=aim, fire=[lined_up, self.fire_slot1(me, dist, lined_up)])

        # Walk to the nearest cube (or a laser/lightning gun if we have a free slot).
        loot = self.nearest_loot(obs, me)
        if loot:
            loot_yaw = yaw_to(me["pos"], loot)
            act["move"] = heading(loot_yaw)
            # Within 45 m we see in every direction, so we can keep aiming at a crate while
            # walking to a cube. Farther than that we only see what we face, so look at the
            # loot or it may vanish from the observation.
            if not target or math.dist(loot, me["pos"]) > 40:
                act["aim"] = [loot_yaw, 0.0]
        elif self.retreating and me["in_zone"]:
            # Healing: walk slowly toward the zone center. 0.4 x 11.7 m/s = 4.7 m/s, just
            # under the 5 m/s where footsteps start making sound, so enemies can't hear us.
            act["move"] = heading(yaw_to(me["pos"], obs["zone"]["next_center"]), 0.4)

        # Supply drop: 3 cubes + full HP + full energy for standing in its ring alone for 4 s.
        # It beats ordinary loot, except a cube or weapon that's almost underfoot (grab that
        # first, it only takes a moment). It also beats slow-walking while retreating, since
        # the drop heals us fully - much faster than regen.
        drop = self.drop_target(obs, me)
        if drop and not (loot and math.dist(loot, me["pos"]) < 15):
            drop_yaw = yaw_to(me["pos"], drop["pos"])
            # Aim for a point well inside the 8 m ring, not its edge, so strafing-sized
            # wobbles don't knock us out of it and reset the capture clock.
            if math.dist(drop["pos"][:2], me["pos"][:2]) > drop["radius"] - 3:
                act["move"] = heading(drop_yaw)
                if not target:
                    act["aim"] = [drop_yaw, 0.0]
            else:
                # Inside the ring: stand still (also silent: no footsteps) and slowly turn
                # the torso in a full circle, so we see anyone sneaking up from behind.
                # 1.5 rad/s means one full look-around every ~4 s, about the capture time.
                act["move"] = [0.0, 0.0]
                if not target:
                    act["aim"] = [(obs["t"] * 1.5) % (2 * math.pi), 0.0]
            mf.draw.point([*drop["pos"][:2], me["pos"][2]], "#ffd060", 8)

        # Your threat logic: hit by someone we can't see in the last 2 s -> turn to face them
        # (rear hits do 1.5x damage, and turning lets us see them). This comes last so it
        # overrides farming, and we stop shooting crates while we turn.
        if self.threat_bearing is not None and obs["t"] - self.threat_time < 2:
            act["aim"] = [self.threat_bearing, 0.0]
            act["fire"] = [False, False]

    # ------------------------------------------------------------------ helpers
    def lead(self, me, target, kind):
        """[yaw, pitch] that leads `target` for our gun's bullet speed, plus the distance."""
        # Your leading math, generalized so it works for mechs, drones and crates.
        x, y, z = me["pos"]
        pos = target["pos"]
        vel = target.get("vel", (0, 0, 0))  # crates have no "vel" -> treat as not moving
        dist = math.dist(pos, me["pos"])
        flight = dist / me["weapons"][0]["stats"]["speed"]  # seconds until the bullet arrives
        # Predicted position = position + velocity * flight time, for x, y and z.
        px, py, pz = (pos[i] + vel[i] * flight for i in range(3))
        # Height difference between the target's middle and our muzzle. The muzzle height
        # comes from our stats because cubes make our mech bigger (and the muzzle higher).
        rise = (pz + CENTER_HEIGHT[kind]) - (z + me["stats"]["muzzle_height"])
        return [math.atan2(py - y, px - x), math.atan2(rise, math.dist((px, py), (x, y)))], dist

    def fire_slot1(self, me, dist, lined_up):
        """Trigger decision for slot 1 (lightning / laser; mortar is never fired)."""
        # This is your Rule 4, moved into its own function so fight() and explore() share it.
        slot1 = me["weapons"][1]
        if slot1 is None:
            return False
        if slot1["kind"] == "lightning":
            # Auto-targets within a 30 deg cone, so no lined_up needed; just range and energy.
            return dist < slot1["stats"]["range"] and me["energy"] > 15
        if slot1["kind"] == "laser":
            if slot1["cooldown"] > 0:
                return False
            if slot1["charge"] > 0:  # charging: keep holding unless full and on target
                return not (slot1["charge"] >= slot1["stats"]["charge"] and lined_up)
            return lined_up and dist < slot1["stats"]["range"]
        return False

    def farm_target(self, obs, me):
        """Nearest drone or reachable crate in FARM_RANGE inside the zone, or None."""
        z = me["pos"][2]
        # Drones live in obs["mechs"] with kind "npc". dict(m, kind=...) makes a copy with a
        # "kind" field, so lead() knows which CENTER_HEIGHT to use (crates have no "kind").
        options = [dict(m, kind="npc") for m in obs["mechs"] if m["kind"] == "npc"]
        # Skip crates on roofs: their cube would drop up there, where we can't reach it.
        options += [dict(c, kind="crate") for c in obs["crates"]
                    if abs(c["pos"][2] - z) < REACH_Z]
        # Keep only close ones inside the zone, then take the nearest.
        options = [o for o in options
                   if math.dist(o["pos"], me["pos"]) < FARM_RANGE and in_zone(o["pos"], obs["zone"])]
        # min(..., default=None) returns None instead of crashing when the list is empty.
        return min(options, key=lambda o: math.dist(o["pos"], me["pos"]), default=None)

    def nearest_loot(self, obs, me):
        """Position of the nearest reachable cube (or wanted weapon if slot 1 is empty)."""
        # Same filters as your cube code, but weapons are included too.
        spots = [c["pos"] for c in obs["cubes"]]
        if me["weapons"][1] is None:
            spots += [p["pos"] for p in obs["pickups"] if p["kind"] in WANTED_WEAPONS]
        z = me["pos"][2]
        spots = [s for s in spots
                 if math.dist(s, me["pos"]) < LOOT_RANGE and abs(s[2] - z) < REACH_Z
                 and in_zone(s, obs["zone"])]
        return min(spots, key=lambda s: math.dist(s, me["pos"]), default=None)

    def drop_target(self, obs, me):
        """The nearest supply drop worth going for, or None.

        A drop is called 25 s before it lands, and every mech can see it in obs["drops"] from
        that moment. We go for one if:
          - it's within DROP_RANGE (walking speed ~11.7 m/s covers ~290 m in those 25 s), and
          - nobody else is capturing it (holder is None, or it's us). If another mech holds
            it, the ring is contested: there's an enemy right there, so it's not free loot.
        """
        options = [d for d in obs["drops"]
                   if math.dist(d["pos"][:2], me["pos"][:2]) < DROP_RANGE
                   and d["holder"] in (None, me["id"])]
        return min(options, key=lambda d: math.dist(d["pos"][:2], me["pos"][:2]), default=None)

    def laser_aimed_at_me(self, obs, me):
        """True if a visible mech is charging a laser pointed roughly at us."""
        for m in obs["mechs"]:
            if m["kind"] == "mech" and m.get("charging"):
                # Compare where THEY aim with the direction from them to us. If those differ by
                # under 0.2 rad (about 11 deg), the beam is coming our way.
                if abs(angle_diff(m["aim"][0], yaw_to(m["pos"], me["pos"]))) < 0.2:
                    return True
        return False

    def take_weapon(self, obs, me, act):
        """Pick up a laser or lightning we're standing on, if slot 1 is free."""
        if me["weapons"][1] is not None:
            return
        x, y, z = me["pos"]
        for p in obs["pickups"]:
            # The game's pickup rule: within pickup_radius horizontally AND 4 m vertically.
            if (p["kind"] in WANTED_WEAPONS and math.dist(p["pos"][:2], (x, y)) < self.reach
                    and abs(p["pos"][2] - z) < REACH_Z):
                act["take"] = p["id"]
                return

    def unstick(self, obs, me, act):
        """If we've pushed into something for 1 s without moving, jump and jet over it."""
        now = obs["t"]
        pushing = math.hypot(*act["move"]) > 0.5  # are we TRYING to move (not slow-walking)?
        speed = math.hypot(me["vel"][0], me["vel"][1])
        if pushing and speed < 1.5 and me["grounded"]:
            # Trying to move but barely moving: start (or continue) the stuck timer.
            if self.stuck_since is None:
                self.stuck_since = now
            if now - self.stuck_since > 1.0:
                # Stuck for over a second: jump, and keep jetting for 1.5 s (below).
                act["jump"] = True
                self.jet_until = now + 1.5
                self.stuck_since = None
        elif speed >= 1.5:
            self.stuck_since = None  # moving fine again: reset the timer
        # Boost while in the air = jets push us UP (about 60 m of climb on a full tank),
        # which carries us over walls and onto roofs. Keep 25 energy in reserve.
        if now < self.jet_until and not me["grounded"] and me["energy"] > 25:
            act["boost"] = True


mf.run(Bot)
