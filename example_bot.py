"""Example bot: walk to the zone, grab a weapon, shoot the nearest mech, draw what it's thinking."""
import math
import mechfight as mf

class Bot:
    name = "example-py"

    def __init__(self, hello: mf.Hello):
        self.reach = hello["config"]["loot"]["pickup_radius"]
        self.threat_bearing = None
        self.threat_time = 0.0
        self.priority = ["armor", "plating", "gun_damage", "nanites", "gun_rate", "gyros"]

    def angle_diff(self, target, current):
        return (target - current + math.pi) % (2 * math.pi) - math.pi
    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = me["pos"]
        goal = obs["zone"]["next_center"]
        act: mf.Action = {"tick": obs["tick"]}
        
        if obs["offer"]:
            top_pick = 10000
            best_index = 0
            for i, choice in enumerate(obs["offer"]["choices"]):
                if choice["id"] in self.priority and self.priority.index(choice["id"]) < top_pick:
                    top_pick = self.priority.index(choice["id"])
                    best_index = i
            act["pick"] = best_index
        near = [p for p in obs["pickups"] if math.dist(p["pos"][:2], (x, y)) < self.reach]
        if me["weapons"][1] is None and near and near[0]["kind"] != "gun":
            act["take"] = near[0]["id"]
        enemies = sorted((m for m in obs["mechs"] if m["kind"] == "mech"), key=lambda m: math.dist(m["pos"], me["pos"]))
        
        yaw = math.atan2(goal[1] - y, goal[0] - x)
        act.update(move=[math.cos(yaw), math.sin(yaw)], aim=[yaw, 0.0], boost=not me["in_zone"], jump=obs["tick"] % 40 == 0)
        if not enemies:
            cubes = sorted((c for c in obs["cubes"] if math.dist(c["pos"], me["pos"]) < 80 and abs(c["pos"][2] - z) < 4 and abs(math.dist(c["pos"][:2], obs["zone"]["center"])) < obs["zone"]["radius"]), key=lambda c: math.dist(c["pos"], me["pos"]))
            if cubes:
                c = cubes[0]["pos"]
                cube_yaw = math.atan2(c[1] - y, c[0] - x)
                act["move"] = [math.cos(cube_yaw), math.sin(cube_yaw)]
                act["aim"] = [cube_yaw, 0.0]
        if me["weapons"][0]["ammo"] < me["weapons"][0]["stats"]["mag"] and not enemies and me["weapons"][0]["reload"] == 0:
            act["reload"] = True
        for event in obs["events"]:
            if event["type"] == "damaged" and event["bearing"] is not None:
                self.threat_bearing = event["bearing"]
                self.threat_time = obs["t"]
        if not enemies and self.threat_bearing is not None and obs["t"] - self.threat_time < 2:
            act["aim"] = [self.threat_bearing, 0.0]
        if enemies:
            t = enemies[0]["pos"]
            e_dist = math.dist(t, (x,y,z))
            bullet_time = e_dist / me["weapons"][0]["stats"]["speed"]
            e_vel = enemies[0]["vel"]
            px = t[0] + e_vel[0] * bullet_time
            py = t[1] + e_vel[1] * bullet_time
            pz = t[2] + e_vel[2] * bullet_time
            aim = [math.atan2(py - y, px - x), math.atan2(pz - z - 2.5, math.dist((px,py), (x, y)))]
            fire = [False, False]
            tolerance = math.atan2(2.0, e_dist)
            lined_up = abs(self.angle_diff(aim[0], me["aim"][0])) < tolerance
            slot1 = me["weapons"][1]
            if lined_up:
                if e_dist < me["weapons"][0]["stats"]["range"]:
                    fire[0] = True
            if slot1 is not None:
                kind = slot1["kind"]
                if kind == "lightning":
                    if e_dist < slot1["stats"]["range"] and me["energy"] > 15:
                        fire[1] = True
                elif kind == "laser":
                    #don't fire if cooldown still active
                    if slot1["cooldown"] > 0:
                        fire[1] = False
                    #if currently charging, keep charging until full and on target
                    elif slot1["charge"] > 0:
                        full_charge = slot1["charge"] >= slot1["stats"]["charge"]
                        fire[1] = not (full_charge and lined_up)
                    #start charge only with good starting conditions
                    else:
                        fire[1] = lined_up and e_dist < slot1["stats"]["range"]
                elif kind == "mortar":
                    fire[1] = False

            act.update(aim=aim, fire=fire)
            mf.draw.line([x, y, z + 7], [t[0], t[1], t[2] + 4.5], "#ff5040")
        
        mf.draw.point([goal[0], goal[1], z], "#40c0ff", 6)
        mf.draw.label(f"hp {me['hp']:.0f} · {len(enemies)} in sight")
        return act


mf.run(Bot)
