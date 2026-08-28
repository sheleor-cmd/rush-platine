#!/usr/bin/env python3
"""Mise à jour automatique du tableau de bord Rush Platine depuis OP.GG.

Pipeline : force le rafraîchissement OP.GG (server action Next.js), attend la fin,
relève rang/LP/bilan + les dernières games (JSON-LD), puis réécrit le bloc DATA
(JSON strict entre __DATA_START__/__DATA_END__) de index.html.
Affiche CHANGED ou UNCHANGED sur stdout ; le workflow ne commit que si CHANGED.
"""
import base64
import io
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
except Exception:
    PARIS = timezone(timedelta(hours=2))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
ACTION_RENEWAL = "405a04669583947dc03eb8c7f367adf28c8f714e86"
ACTION_STATUS = "400c02bdfd8c90756a329b312a7455e73880ad43ec"
ACTION_GAMES = "409a2b9ca50d15e50a4dace93552e3a40113dc2753"
ACTION_SUMMARY = "4028494596c44675d8e9f617b8f659312f3b678072"
CACHE_FILE = "cache.json"
START_ABS = 300
GOAL_ABS = 1600
TIERS = ["Iron", "Bronze", "Silver", "Gold", "Platinum", "Emerald", "Diamond"]
TIERS_FR = {"Iron": "Fer", "Bronze": "Bronze", "Silver": "Argent", "Gold": "Or",
            "Platinum": "Platine", "Emerald": "Émeraude", "Diamond": "Diamant"}
ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV"}
MONTHS_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin",
             "juil.", "août", "sept.", "oct.", "nov.", "déc."]
DIV_FR = []
for _t in ["Fer", "Bronze", "Argent", "Or"]:
    for _d in ["IV", "III", "II", "I"]:
        DIV_FR.append(f"{_t} {_d}")
DIV_FR.append("Platine IV")

PLAYERS = [
    {"key": "louis", "slug": "peixoto123-99999",
     "puuid": "cyUrG_6BtTDFMoeNQZa3r4jpRCU0jb0uTYEANC9hU3NkxMAwyaLJlV1G7LNAErZG9Uc-JcTSN6itxg",
     "start": datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)},
    {"key": "thomas", "slug": "blackstar19998-EUW",
     "puuid": "Od4GBZSMD0OqA7Y7ou3T6shw5Ts6nmaq22LhR6z8xpylcDqbXQNh39aSwS6COdk1nTtj8p_BHf-R9g",
     "start": datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)},
]


def http(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def opgg_action(slug, action_id, payload):
    url = f"https://op.gg/lol/summoners/euw/{slug}"
    headers = {
        "User-Agent": UA, "Next-Action": action_id,
        "Content-Type": "text/plain;charset=UTF-8", "Accept": "text/x-component",
        "Origin": "https://op.gg", "Referer": url,
    }
    return http(url, data=json.dumps(payload).encode(), headers=headers).decode("utf-8", "replace")


def renew(player):
    try:
        opgg_action(player["slug"], ACTION_RENEWAL,
                    [{"region": "euw", "puuid": player["puuid"], "isPremiumPrimary": False}])
    except Exception as e:
        print(f"renewal {player['key']} failed: {e}", file=sys.stderr)
        return
    deadline = time.time() + 150
    while time.time() < deadline:
        try:
            out = opgg_action(player["slug"], ACTION_STATUS,
                              [{"region": "euw", "puuid": player["puuid"]}])
            if "RENEWAL_FINISH" in out:
                return
        except Exception:
            pass
        time.sleep(5)
    print(f"renewal {player['key']}: timeout, on continue", file=sys.stderr)


def fetch_profile(player):
    url = f"https://op.gg/lol/summoners/euw/{player['slug']}?v={int(time.time())}"
    html = http(url).decode("utf-8", "replace")
    m = re.search(
        r'content="[^"]*?/\s*(Iron|Bronze|Silver|Gold|Platinum|Emerald|Diamond)\s+(\d)\s+(\d+)\s+LP\s*/\s*(\d+)Win\s+(\d+)Lose',
        html)
    if not m:
        raise RuntimeError(f"rang introuvable pour {player['key']}")
    tier, div, lp, wins, losses = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
    abs_lp = TIERS.index(tier) * 400 + (4 - div) * 100 + lp
    games = []
    ld = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    if ld:
        try:
            data = json.loads(ld.group(1))
            for node in data.get("@graph", []):
                if node.get("@type") == "ItemList":
                    for it in node.get("itemListElement", []):
                        item = it.get("item", {})
                        props = {p["name"]: p.get("value") for p in item.get("additionalProperty", [])}
                        if props.get("queueType") == "SOLORANKED":
                            games.append({
                                "champ": props.get("champion"),
                                "win": props.get("result") == "WIN",
                                "k": int(props.get("kills", 0)), "d": int(props.get("deaths", 0)),
                                "a": int(props.get("assists", 0)),
                            })
        except Exception as e:
            print(f"json-ld {player['key']}: {e}", file=sys.stderr)
    return {"tier": tier, "div": div, "lp": lp, "abs": abs_lp,
            "wins": wins, "losses": losses, "recent": games}


CHALLENGE_CUTOFF = "2026-08-23T00:00:00+00:00"


def fetch_games(player):
    """TOUTES les games solo/duo du défi, avec les 10 participants (getGames paginé via endedAt)."""
    games, ended_at = [], ""
    for _ in range(30):  # garde-fou : 30 pages × ~20 games
        try:
            out = opgg_action(player["slug"], ACTION_GAMES,
                              [{"locale": "en", "region": "euw", "puuid": player["puuid"],
                                "gameType": "soloranked", "endedAt": ended_at, "champion": ""}])
        except Exception as e:
            print(f"getGames {player['key']}: {e}", file=sys.stderr)
            break
        batch = []
        for line in out.split("\n"):
            if line.startswith("1:"):
                try:
                    batch = json.loads(line[2:]).get("data", [])
                except Exception as e:
                    print(f"getGames parse {player['key']}: {e}", file=sys.stderr)
                break
        if not batch:
            break
        games.extend(g for g in batch
                     if (g.get("game_type") or {}).get("game_type") == "SOLORANKED"
                     and g.get("created_at", "9999") >= "2026-08-23")
        last = batch[-1].get("created_at", "")
        if not last or last < "2026-08-23":
            break
        ended_at = last
        time.sleep(1)
    return games


def compute_tribunal(games, my_puuid):
    """Statistiques (à charge et à décharge) sur les botlanes alliées."""
    n = bk = bd = ba = ek = ed = ea = fus = botwin = mvp = 0
    lanes, ranks = [], []
    worst, worst_kda = None, None
    for g in games:
        if (g.get("game_length") or 0) < 600:  # remake
            continue
        team = g.get("team_red") if g.get("summoner_team") == "RED" else g.get("team_blue")
        enemy = g.get("team_blue") if g.get("summoner_team") == "RED" else g.get("team_red")
        ally_bot = [m for m in (team or []) if m.get("position") in ("ADC", "SUPPORT")
                    and (m.get("summoner") or {}).get("puuid") != my_puuid]
        en_bot = [m for m in (enemy or []) if m.get("position") in ("ADC", "SUPPORT")]
        me = next((m for m in (team or []) if (m.get("summoner") or {}).get("puuid") == my_puuid), None)
        if len(ally_bot) < 2:
            continue
        n += 1
        k = sum(m["stats"]["kill"] for m in ally_bot)
        d = sum(m["stats"]["death"] for m in ally_bot)
        a = sum(m["stats"]["assist"] for m in ally_bot)
        bk += k; bd += d; ba += a
        game_kda = (k + a) / max(1, d)
        if game_kda < 1:
            fus += 1
        duo_lanes = [(m.get("stats") or {}).get("lane_score") for m in ally_bot]
        duo_lanes = [x for x in duo_lanes if isinstance(x, (int, float))]
        lanes.extend(duo_lanes)
        # « pire botlane » : le pire K/D du duo (kills / morts, les assists ne comptent pas)
        duo_kd = k / max(1, d)
        if worst_kda is None or duo_kd < worst_kda or (duo_kd == worst_kda and worst and d > int(worst["kda"].split("/")[1])):
            worst_kda = duo_kd
            worst = {"champs": " + ".join((m.get("champion") or {}).get("name", "?") for m in ally_bot),
                     "kda": f"{k}/{d}/{a}",
                     "lane": round(sum(duo_lanes) / len(duo_lanes)) if duo_lanes else None}
        if en_bot:
            ke = sum(m["stats"]["kill"] for m in en_bot)
            de = sum(m["stats"]["death"] for m in en_bot)
            ae = sum(m["stats"]["assist"] for m in en_bot)
            ek += ke; ed += de; ea += ae
            if game_kda >= (ke + ae) / max(1, de):
                botwin += 1
        st = g.get("stats") or {}
        if me and (me.get("stats") or {}).get("is_opscore_max_in_team"):
            mvp += 1
        if isinstance(st.get("op_score_rank"), (int, float)):
            ranks.append(st["op_score_rank"])
    if n == 0:
        return None
    bot_kda = round((bk + ba) / max(1, bd), 2)
    enemy_kda = round((ek + ea) / max(1, ed), 2)
    fus_pct = round(fus / n * 100)
    mvp_pct = round(mvp / n * 100)
    avg_rank = round(sum(ranks) / len(ranks), 1) if ranks else None
    if fus_pct >= 30 and mvp_pct >= 30:
        verdict, tone = "Plainte recevable : enfer botlane confirmé", "bad"
    elif bot_kda < enemy_kda and (avg_rank or 10) <= 4:
        verdict, tone = "Les stats donnent (un peu) raison à la plainte", "mid"
    elif (avg_rank or 0) >= 5.5:
        verdict, tone = "Plainte rejetée : le plaignant n'est pas irréprochable", "good"
    else:
        verdict, tone = "Classé sans suite : botlanes dans la moyenne", "mid"
    return {
        "sample": n, "botKda": bot_kda, "enemyBotKda": enemy_kda,
        "botLane": round(sum(lanes) / len(lanes)) if lanes else None,
        "fusPct": fus_pct, "botWinPct": round(botwin / n * 100),
        "worst": worst, "mvpPct": mvp_pct, "avgRank": avg_rank,
        "verdict": verdict, "tone": tone,
    }


def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cache, f, ensure_ascii=False)


def fetch_winrate(puuid, slug):
    """Winrate solo/duo global d'un joueur (agrégat toutes-champions du résumé ranked)."""
    try:
        out = opgg_action(slug, ACTION_SUMMARY,
                          [{"region": "euw", "puuid": puuid, "locale": "en"}])
        for line in out.split("\n"):
            if line.startswith("1:"):
                for it in json.loads(line[2:]) or []:
                    if it.get("is_all_champions"):
                        return {"wr": it.get("win_rate"), "play": it.get("play")}
    except Exception as e:
        print(f"winrate {puuid[:12]}: {e}", file=sys.stderr)
    return None


def tier_abs(ti):
    if not ti or ti.get("tier") not in [t.upper() for t in TIERS]:
        return None
    tier = ti["tier"].capitalize() if ti["tier"].capitalize() in TIERS else ti["tier"].title()
    try:
        idx = [t.upper() for t in TIERS].index(ti["tier"])
    except ValueError:
        return None
    return idx * 400 + (4 - int(ti.get("division") or 4)) * 100 + int(ti.get("lp") or 0)


def compute_lobby(games, my_puuid, slug, cache):
    """Rang moyen + winrate moyen des alliés et des adversaires sur toutes les games."""
    sides = {"ally": {"abs": [], "wr": []}, "enemy": {"abs": [], "wr": []}}
    for g in games:
        if (g.get("game_length") or 0) < 600:
            continue
        team = g.get("team_red") if g.get("summoner_team") == "RED" else g.get("team_blue")
        enemy = g.get("team_blue") if g.get("summoner_team") == "RED" else g.get("team_red")
        for side, members in (("ally", team), ("enemy", enemy)):
            for m in (members or []):
                pu = (m.get("summoner") or {}).get("puuid")
                if not pu or pu == my_puuid:
                    continue
                ab = tier_abs(m.get("tier_info"))
                if ab is not None:
                    sides[side]["abs"].append(ab)
                ent = cache.get(pu)
                now_t = time.time()
                stale = (ent is None or "t" not in ent
                         or (ent.get("wr") is None and now_t - ent["t"] > 86400)
                         or now_t - ent.get("t", 0) > 7 * 86400)
                if stale:
                    ent = dict(fetch_winrate(pu, slug) or {})
                    ent["t"] = int(now_t)
                    cache[pu] = ent
                    time.sleep(0.2)
                wr = (ent or {}).get("wr")
                if isinstance(wr, (int, float)):
                    sides[side]["wr"].append(wr)
    def pack(s):
        if not s["abs"]:
            return None
        avg = sum(s["abs"]) / len(s["abs"])
        return {"rank": DIV_FR[min(int(avg // 100), len(DIV_FR) - 1)],
                "wr": round(sum(s["wr"]) / len(s["wr"])) if s["wr"] else None}
    a, e = pack(sides["ally"]), pack(sides["enemy"])
    if not a or not e:
        return None
    return {"ally": a, "enemy": e}


def compute_fun(games, my_puuid):
    """Stats cumulées rigolotes sur toutes les games du défi."""
    t = {"k": 0, "d": 0, "a": 0, "sec": 0, "dead": 0.0, "cs": 0, "gold": 0, "dmg": 0,
         "wards": 0, "wkill": 0, "spree": 0, "multi": 0, "longest": 0}
    for g in games:
        L = g.get("game_length") or 0
        if L < 600:
            continue
        team = g.get("team_red") if g.get("summoner_team") == "RED" else g.get("team_blue")
        me = next((m for m in (team or [])
                   if (m.get("summoner") or {}).get("puuid") == my_puuid), None)
        s = (me or {}).get("stats") or {}
        t["k"] += s.get("kill", 0); t["d"] += s.get("death", 0); t["a"] += s.get("assist", 0)
        t["sec"] += L
        # timer de mort moyen ~ 5 s + 0,7 s par minute de jeu (estimation)
        t["dead"] += s.get("death", 0) * (5 + 0.7 * L / 60)
        t["cs"] += s.get("minion_kill", 0) + (s.get("neutral_minion_kill") or 0)
        t["gold"] += s.get("gold_earned", 0)
        t["dmg"] += s.get("total_damage_dealt_to_champions", 0)
        t["wards"] += s.get("ward_place", 0); t["wkill"] += s.get("ward_kill", 0)
        t["spree"] = max(t["spree"], s.get("largest_killing_spree", 0))
        t["multi"] = max(t["multi"], s.get("largest_multi_kill", 0))
        t["longest"] = max(t["longest"], L)
    if t["sec"] == 0:
        return None
    h, m = divmod(int(t["sec"] // 60), 60)
    dh, dm = divmod(int(t["dead"] // 60), 60)
    multi_lbl = {0: "—", 1: "Solo kill", 2: "Double kill", 3: "Triple kill",
                 4: "Quadra kill", 5: "PENTAKILL"}.get(t["multi"], "—")
    return {"kills": t["k"], "deaths": t["d"], "assists": t["a"],
            "ingame": f"{h} h {m:02d}",
            "dead": (f"{dh} h {dm:02d}" if dh else f"{dm} min"),
            "cs": t["cs"], "gold": t["gold"], "ie": round(t["gold"] / 3400),
            "dmg": t["dmg"], "teemos": round(t["dmg"] / 598),
            "wards": t["wards"], "wardKills": t["wkill"],
            "spree": t["spree"], "multi": multi_lbl,
            "longest": round(t["longest"] / 60)}


def rebuild_from_games(stored, games, my_puuid):
    """Reconstruit forme, pool de champions, KDA et pick signature depuis l'historique complet.

    Source de vérité par game (résultat, champion, K/D/A du joueur) — élimine toute
    dérive des mises à jour incrémentales. Retourne la liste des champions joués.
    """
    ordered = sorted((g for g in games if (g.get("game_length") or 0) >= 600),
                     key=lambda g: g.get("created_at", ""))
    form, champs = [], {}
    K = D = A = sec = cs = 0
    lane_scores, kps = [], []
    for g in ordered:
        team = g.get("team_red") if g.get("summoner_team") == "RED" else g.get("team_blue")
        me = next((m for m in (team or [])
                   if (m.get("summoner") or {}).get("puuid") == my_puuid), None)
        if not me:
            continue
        s = me.get("stats") or {}
        name = (me.get("champion") or {}).get("name") or "?"
        win = g.get("game_result") == "WIN"
        form.append("V" if win else "D")
        c = champs.setdefault(name, {"n": name, "g": 0, "w": 0, "l": 0, "k": 0, "d": 0, "a": 0, "kda": 0})
        c["g"] += 1
        c["w" if win else "l"] += 1
        c["k"] += s.get("kill", 0); c["d"] += s.get("death", 0); c["a"] += s.get("assist", 0)
        K += s.get("kill", 0); D += s.get("death", 0); A += s.get("assist", 0)
        sec += g.get("game_length") or 0
        cs += s.get("minion_kill", 0) + (s.get("neutral_minion_kill") or 0)
        if isinstance(s.get("lane_score"), (int, float)):
            lane_scores.append(s["lane_score"])
        team_kills = sum((mm.get("stats") or {}).get("kill", 0) for mm in (team or []))
        if team_kills > 0:
            kps.append((s.get("kill", 0) + s.get("assist", 0)) / team_kills * 100)
    if not form:
        return []
    if sec:
        stored["csMin"] = round(cs / (sec / 60), 1)
        stored["avgLen"] = round(sec / len(form) / 60)
    if lane_scores:
        stored["laneScore"] = round(sum(lane_scores) / len(lane_scores))
    if kps:
        stored["kp"] = round(sum(kps) / len(kps))
    for c in champs.values():
        c["kda"] = round((c["k"] + c["a"]) / max(1, c["d"]), 2)
    lst = sorted(champs.values(), key=lambda c: (-c["g"], -c["w"], c["n"]))
    stored["form"] = form
    stored["champs"] = lst
    stored["k"], stored["d"], stored["a"] = K, D, A
    stored["kda"] = round((K + A) / max(1, D), 2)
    stored["streak"], stored["bestStreak"] = streak_fr(form)
    best = max(lst, key=lambda c: (c["w"], c["w"] / max(1, c["g"]), c["kda"]))
    stored["signature"] = {"champ": best["n"],
                           "note": f"{best['w']}V {best['l']}D · {best['kda']:.2f} de KDA"}
    return [c["n"] for c in lst]


def streak_fr(form):
    if not form:
        return "—", "—"
    last, n = form[-1], 0
    for g in reversed(form):
        if g != last:
            break
        n += 1
    cur = f"{n} {'victoire' if last == 'V' else 'défaite'}{'s' if n > 1 else ''}"
    best, run = 0, 0
    for g in form:
        run = run + 1 if g == "V" else 0
        best = max(best, run)
    return cur, f"{best} victoire{'s' if best > 1 else ''}"


_ddragon = {}


def champ_icon_b64(name):
    try:
        from PIL import Image
        versions = json.loads(http("https://ddragon.leagueoflegends.com/api/versions.json"))
        if not _ddragon:
            meta = json.loads(http(
                f"https://ddragon.leagueoflegends.com/cdn/{versions[0]}/data/en_US/champion.json"))
            for cid, c in meta.get("data", {}).items():
                _ddragon[c.get("name", cid)] = cid
        cid = _ddragon.get(name, name)
        png = http(f"https://ddragon.leagueoflegends.com/cdn/{versions[0]}/img/champion/{cid}.png")
        img = Image.open(io.BytesIO(png)).convert("RGB").resize((48, 48), Image.BICUBIC)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=82)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"icone {name}: {e}", file=sys.stderr)
        return None


def main():
    with open("index.html", encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"/\*__DATA_START__\*/(.*?)/\*__DATA_END__\*/", html, re.S)
    if not m:
        raise RuntimeError("marqueurs DATA introuvables")
    data = json.loads(m.group(1))

    for p in PLAYERS:
        renew(p)

    now = datetime.now(timezone.utc)
    changed = False

    for p, stored in zip(PLAYERS, data["players"]):
        live = fetch_profile(p)
        if (live["lp"] == stored["lp"] and live["wins"] == stored["wins"]
                and live["losses"] == stored["losses"]):
            continue
        changed = True
        delta = (live["wins"] + live["losses"]) - stored["games"]
        # nouvelles games, en ordre chronologique
        known = list(reversed(live["recent"][:max(0, min(delta, len(live["recent"])))])) if delta > 0 else []
        extra_w = (live["wins"] - stored["wins"]) - sum(1 for g in known if g["win"])
        extra_l = (live["losses"] - stored["losses"]) - sum(1 for g in known if not g["win"])
        pad = [{"champ": None, "win": True, "k": 0, "d": 0, "a": 0}] * max(0, extra_w) + \
              [{"champ": None, "win": False, "k": 0, "d": 0, "a": 0}] * max(0, extra_l)
        new_games = (pad + known)[-delta:] if delta > 0 else []
        for g in new_games:
            stored["form"].append("V" if g["win"] else "D")
            stored["k"] += g["k"]; stored["d"] += g["d"]; stored["a"] += g["a"]
            if g["champ"]:
                entry = next((c for c in stored["champs"] if c["n"] == g["champ"]), None)
                if entry is None:
                    entry = {"n": g["champ"], "g": 0, "w": 0, "l": 0, "k": 0, "d": 0, "a": 0, "kda": 0}
                    stored["champs"].append(entry)
                    icon = champ_icon_b64(g["champ"])
                    if icon and f'"{g["champ"]}":' not in html:
                        html = html.replace("const CHAMP_IMG = {",
                                            'const CHAMP_IMG = {\n  "%s": "%s",' % (g["champ"], icon), 1)
                entry["g"] += 1
                entry["w" if g["win"] else "l"] += 1
                entry["k"] += g["k"]; entry["d"] += g["d"]; entry["a"] += g["a"]
                entry["kda"] = round((entry["k"] + entry["a"]) / max(1, entry["d"]), 2)

        stored["rank"] = f"{TIERS_FR[live['tier']]} {ROMAN[live['div']]}"
        stored["lp"] = live["lp"]; stored["abs"] = live["abs"]
        stored["wins"] = live["wins"]; stored["losses"] = live["losses"]
        stored["games"] = live["wins"] + live["losses"]
        stored["kda"] = round((stored["k"] + stored["a"]) / max(1, stored["d"]), 2)
        gained = live["abs"] - START_ABS
        stored["lpPerGame"] = f"+{gained / stored['games']:.1f}"
        days = max(0.05, (now - p["start"]).total_seconds() / 86400)
        pace = gained / days
        stored["pace"] = f"+{pace:.0f} LP/jour"
        stored["paceOver"] = f"{days:.1f} j".replace(".", ",")
        if live["abs"] >= GOAL_ABS:
            stored["eta"] = "objectif atteint 🏆"
            stored["nextMilestone"] = "Platine atteint !"
        else:
            eta = now + timedelta(days=(GOAL_ABS - live["abs"]) / max(1.0, pace))
            eta_p = eta.astimezone(PARIS)
            stored["eta"] = f"{eta_p.day}{'er' if eta_p.day == 1 else ''} {MONTHS_FR[eta_p.month - 1]}"
            nxt = DIV_FR[min(live["abs"] // 100 + 1, len(DIV_FR) - 1)]
            stored["nextMilestone"] = f"{nxt} dans {100 - live['lp']} LP"
        stored["streak"], stored["bestStreak"] = streak_fr(stored["form"])

    need_seed = "tribunal" not in data or "lobby" not in data or "fun" not in data
    if changed or need_seed:
        cache = load_cache()
        stored_by_key = {s["key"]: s for s in data["players"]}
        trib, lobby, fun = {}, {}, {}
        for p in PLAYERS:
            games = fetch_games(p)
            t = compute_tribunal(games, p["puuid"])
            if t:
                trib[p["key"]] = t
            lb = compute_lobby(games, p["puuid"], p["slug"], cache)
            if lb:
                lobby[p["key"]] = lb
            fn = compute_fun(games, p["puuid"])
            if fn:
                fun[p["key"]] = fn
            # reconstruction exacte (forme, pool, KDA, signature) + icônes manquantes
            for name in rebuild_from_games(stored_by_key[p["key"]], games, p["puuid"]):
                if f'"{name}":' not in html:
                    icon = champ_icon_b64(name)
                    if icon:
                        html = html.replace("const CHAMP_IMG = {",
                                            'const CHAMP_IMG = {\n  "%s": "%s",' % (name, icon), 1)
        save_cache(cache)
        for key, val in (("tribunal", trib), ("lobby", lobby), ("fun", fun)):
            if val:
                data[key] = val
                changed = True

    if not changed:
        print("UNCHANGED")
        return

    # le duel
    def set_duel(label, a, b):
        for row in data["duel"]:
            if row["label"] == label:
                row["vals"] = [a, b]

    lo, th = data["players"]
    d_lo = max(0.05, (now - PLAYERS[0]["start"]).total_seconds() / 86400)
    d_th = max(0.05, (now - PLAYERS[1]["start"]).total_seconds() / 86400)
    set_duel("Games par jour", round(lo["games"] / d_lo, 1), round(th["games"] / d_th, 1))
    set_duel("KDA", lo["kda"], th["kda"])
    set_duel("LP par game", round((lo["abs"] - START_ABS) / lo["games"], 1),
             round((th["abs"] - START_ABS) / th["games"], 1))
    set_duel("LP gagnés", lo["abs"] - START_ABS, th["abs"] - START_ABS)
    b_lo = int(lo["bestStreak"].split()[0]); b_th = int(th["bestStreak"].split()[0])
    set_duel("Meilleure série", b_lo, b_th)
    if lo.get("avgLen") and th.get("avgLen"):
        set_duel("Durée moyenne", lo["avgLen"], th["avgLen"])
    if lo.get("kp") is not None and th.get("kp") is not None:
        set_duel("Participation aux kills", lo["kp"], th["kp"])
    if lo.get("laneScore") is not None and th.get("laneScore") is not None:
        set_duel("Score de lane", lo["laneScore"], th["laneScore"])

    now_p = now.astimezone(PARIS)
    full_fr = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
               "août", "septembre", "octobre", "novembre", "décembre"]
    data["updated"] = f"{now_p.day} {full_fr[now_p.month - 1]} {now_p.year}, {now_p.hour}h{now_p.minute:02d}"

    blob = json.dumps(data, ensure_ascii=False, indent=2)
    html = re.sub(r"/\*__DATA_START__\*/.*?/\*__DATA_END__\*/",
                  "/*__DATA_START__*/" + blob.replace("\\", "\\\\") + "/*__DATA_END__*/",
                  html, count=1, flags=re.S)
    with open("index.html", "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    print("CHANGED")


if __name__ == "__main__":
    main()
