""" Tournament cups (PvE): the World Cup / European Championship engine.

Split out of http_config.py (2026-11-26). That file is the upstream server's single 10k-line
script -- every feature added on top of it used to land in the middle of it. Everything in this
module is ours; the upstream cup code (the club cups: new_cup, cup_play_round, cup_refresh_stats,
the /cup handlers) stays in http_config and is used from here through `hc`.

An event is data: one entry of config/tournaments.json -- metaid, name, the seeding pots, the card
sources a side is built from, and the format. This module turns that entry into a cup-shaped
instance the client already knows how to draw (CupProxy / CupMainScreen), picks both squads, plays
the AI rounds and builds the knock-out bracket.
"""
import copy
import json
import os
import random
import time
from contextlib import closing

import gen_teams


class _Host:
    """http_config, resolved on first use.

    http_config imports this module in the middle of its own body (right where this block used to
    live), so a plain `import http_config` at the top here would hand back a half-built module --
    and `import modules.tournaments` before http_config would deadlock the other way round. Going
    through __getattr__ makes both orders work: nothing is read off the host until a request calls
    in, and by then http_config has finished importing."""

    def __getattr__(self, name):
        import http_config  # noqa: PLC0415 -- the point: late, never at module import

        return getattr(http_config, name)


hc = _Host()

TOURNAMENT_CONFIG = ("config", "tournaments.json")


def tournament_config_file():
    """config/tournaments.json, resolved per call: importing this module must not read anything
    off the host (see _Host)."""
    return os.path.join(hc.HERE, *TOURNAMENT_CONFIG)


# The event's own card sources are part of the event, not of these defaults: 14W is season 77,
# 2018 is another season code, and a config that puts one of them here would field 14W players in
# every future tournament. A cup that says nothing falls back to the plain current pool.
TOURNAMENT_DEFAULTS = {"card_sources": [{"current": True}],
                       "squad_size": gen_teams.SELECTABLE, "min_players": 11, "type": "wcpve",
                       # "authentic" = the player takes over a licensed side, so the team picker
                       # previews THAT side's squad instead of the coach's own club (see
                       # ep_authentic_team); "user" would lock the coach's club, "nation" opens the
                       # same picker but previews his own players. Per cup override as usual.
                       "teamselect": "authentic", "trophy": "66", "skin": "66", "quality": 4,
                       # card_grade is filled in by tournament_config(): it is the host's
                       # CUP_AI_GRADE, and nothing may read the host at import time (see _Host).
                       "matchcount": 7, "reward": 6000000, "format": {}}
_TOUR_CACHE = {"mtime": None, "cups": []}
_TOUR_SQUADS = {}


def tournament_config():
    """The enabled cups of config/tournaments.json with the defaults merged in, re-read when the
    file's mtime changes: a config edit needs no server restart for the NEXT cup created. A cup
    already in progress keeps the copy of the format stored on it (cup["format"])."""
    try:
        mtime = os.stat(tournament_config_file()).st_mtime
    except OSError:
        return []
    if _TOUR_CACHE["mtime"] != mtime:
        with open(tournament_config_file(), encoding="utf-8") as f:
            cfg = json.load(f)
        base = dict(TOURNAMENT_DEFAULTS, **(cfg.get("defaults") or {}))
        base.setdefault("card_grade", hc.CUP_AI_GRADE)  # a config that says nothing: +1 like the AI
        cups = []
        for e in cfg.get("cups") or []:
            if not e.get("enabled", True):
                continue
            d = dict(base, **{k: v for k, v in e.items() if not k.startswith("_")})
            d["format"] = dict(base.get("format") or {}, **(e.get("format") or {}))
            cups.append(d)
        _TOUR_CACHE.update(mtime=mtime, cups=cups)
        _TOUR_SQUADS.clear()
    return _TOUR_CACHE["cups"]


def tournament_entry(metaid):
    """One configured cup by metaid, or None."""
    try:
        metaid = int(metaid or 0)
    except (TypeError, ValueError):
        return None
    return next((e for e in tournament_config() if int(e.get("metaid") or 0) == metaid), None)


def tournament_teamids(entry=None):
    """The national teamids a configured cup fields, pot by pot (pot 1 = the seeds first, which is
    the order the team wheel shows). entry=None gives the whole configured set. The pots, not
    gen_teams.club_index, are the authority: club_index drops any side whose 14W cards lost
    nationalteamid (Brazil), and they are still in the draw."""
    out = []
    for e in ([entry] if entry is not None else tournament_config()):
        for pot in e.get("pots") or ():
            for t in pot.get("teams") or ():
                tid = int(t[0])
                if tid not in out:
                    out.append(tid)
    return out


def tournament_entry_of_team(teamid):
    """The configured cup that fields this national team, or None (a club id)."""
    return next((e for e in tournament_config() if int(teamid) in tournament_teamids(e)), None)


def tournament_meta(entry, list_type="wcpve"):
    """A /cup/list entry for a tournament. list_type="pve" is what CupFrontScreen draws (the
    instance itself stays "wcpve", which is what ep_cup_next/ep_cup_match branch on);
    teamselect != "user" is what makes CupController open the 3-wheel nation picker instead of
    locking the coach's own club ("authentic" additionally makes the picker preview the picked
    team's own squad, and hides the squad screen's contract-extension button).

    joinable is not just a fee/flag bag: CupProxy hands it to TeamSelectionUtil.rearrangeData as
    the picker's whitelist, which runs only while joinable.allteams is falsy and then keeps just
    joinable.teamids. Leaving it true hands the picker SORTTEAM's whole league-78 list (every
    nation with a playable squad plus the cups' pot teams, Brazil included), which is what a
    private server wants: pick China or India and the draw files you in pot 4. A cup that should
    only offer its own participants -- the real 32-team World Cup -- sets joinable_teams="pots"."""
    n = sum(len(p.get("teams") or ()) for p in entry.get("pots") or ())
    meta = hc.cup_meta(entry["metaid"], entry["name"], entry.get("trophy"), entry.get("quality"),
                    entry.get("matchcount"), n or 32, entry.get("reward"))
    teams = tournament_teamids(entry)
    pots_only = teams and entry.get("joinable_teams") == "pots"
    joinable = dict(meta["joinable"], allteams=False, teamids=teams) if pots_only else meta["joinable"]
    return dict(meta, type=list_type, teamselect=entry.get("teamselect"), skin=entry.get("skin"),
                joinable=joinable)


def cup_or_tournament_meta(metaid):
    """The /cup/list entry of any cup id: a club cup from CUPS, or a configured tournament (list
    type "pve"; the instance itself is "wcpve"). Callers only read trophy / skin / prize off it."""
    meta = next((m for m in hc.CUPS if m["metaid"] == metaid), None)
    if meta is not None:
        return meta
    e = tournament_entry(metaid)
    return tournament_meta(e, "pve") if e else None


def tournament_card_sources(entry, teamid):
    """The card sources of one tournament team: its own override in the cup config, else the cup's
    (the event's own card season first, topped up with the current pool), else the current pool."""
    ov = ((entry or {}).get("team_overrides") or {}).get(str(teamid)) or {}
    return (ov.get("card_sources") or (entry or {}).get("card_sources")
            or TOURNAMENT_DEFAULTS["card_sources"])


def tournament_roster(teamid, entry=None):
    """{summary, team} of a tournament side, or None when even the sources cannot make 11 players
    for it: the configured card sources, so both my side and the opponents field the real players
    of that competition instead of the best cards this DB happens to have for that nation. Picked
    by nationality (see gen_teams._team_filter), so a nation whose cards lost nationalteamid still
    gets its full set."""
    with closing(hc._db()) as c:
        return gen_teams.team(c, teamid, tournament_card_sources(entry, teamid), national=True,
                              by_nation=True,
                              min_players=int((entry or {}).get("squad_size") or gen_teams.SELECTABLE),
                              min_lines=(entry or {}).get("squad_lines"))


def tournament_roster_team(teamid, entry=None):
    """tournament_roster's team half for a match side: a nation the sources cannot fill falls back
    to its plain roster rather than folding an empty squad into a match."""
    return (tournament_roster(teamid, entry) or {}).get("team") or hc.roster_team(teamid)


def tournament_squad(entry, teamid):
    """tournament_roster_team, cached per config revision + team: the draw, the strength estimates
    and the match sides ask for the same 32 squads over and over."""
    tournament_config()
    key = (_TOUR_CACHE["mtime"], int((entry or {}).get("metaid") or 0), teamid)
    if key not in _TOUR_SQUADS:
        _TOUR_SQUADS[key] = tournament_roster_team(teamid, entry)
    return _TOUR_SQUADS[key]


def tournament_strength(entry, teamid):
    """Best-XI average of a tournament squad -- the counterpart of team_strength(), which reads
    the plain club/nation roster and would rate a 14W side by its current-season cards."""
    ratings = sorted((p.get("price") or 0 for p in tournament_squad(entry, teamid).get("players") or ()),
                     reverse=True)
    return sum(ratings[:11]) / 11 if len(ratings) >= 11 else 60


def tournament_draw(entry, mine_teamid, seed=None):
    """The teams of one tournament, group by group: every group gets exactly one team per pot.

    The player's nation keeps its pot when the cup knows it (Brazil stays a seed, and its group
    still gets a pot-2, a pot-3 and a pot-4 side). A nation the cup does not know did not qualify,
    so it is a pot-4 team: it takes the place of a random pot-4 side.

    A team whose cards cannot make `min_players` players is replaced by one of `reserves` inside
    its own pot -- a fifadb without that nation would otherwise fold an empty squad into the cup.
    The pots themselves already carry a substitute for every nation the client cannot name (its
    TEAMNAMES_CHI only has 47 national teams), so this is the second line of defence."""
    fmt = entry.get("format") or {}
    names = fmt.get("group_names") or "ABCDEFGH"
    pots = [[(int(t[0]), str(t[1]) if len(t) > 1 else "") for t in (p.get("teams") or ())]
            for p in entry.get("pots") or ()]
    if len(pots) < 2 or any(len(p) != len(names) for p in pots):
        raise ValueError("tournaments.json: every pot needs exactly len(group_names) teams")
    floor = int(entry.get("min_players") or 11)
    rng = random.Random(seed)
    reserves = [(int(t[0]), str(t[1]) if len(t) > 1 else "") for t in entry.get("reserves") or ()]
    used = {t[0] for pot in pots for t in pot}
    dealt = []
    for pot in pots:
        row = []
        for team in pot:
            if len(tournament_squad(entry, team[0]).get("players") or ()) >= floor:
                row.append(team)
                continue
            sub = next((t for t in reserves if t[0] not in used), None)
            if sub is None:
                row.append(team)  # nothing left to swap in: its plain roster still has 11+
                continue
            used.add(sub[0])
            row.append(sub)
        dealt.append(row)
    mine = int(mine_teamid or 0)
    my_pot = next((i for i, pot in enumerate(dealt) if any(t[0] == mine for t in pot)), None)
    if my_pot is None:  # an outsider: pot 4, replacing a random pot-4 team
        my_pot = len(dealt) - 1
        dealt[my_pot][rng.randrange(len(dealt[my_pot]))] = (mine, "")
    my_group = rng.randrange(len(names))
    groups = [{} for _ in names]
    for pi, pot in enumerate(dealt):
        teams = [t for t in pot if t[0] != mine]
        rng.shuffle(teams)
        slots = [g for g in range(len(names)) if not (pi == my_pot and g == my_group)]
        for g, team in zip(slots, teams):
            groups[g][pi] = team
        if pi == my_pot:
            groups[my_group][pi] = (mine, "")
    return [[groups[g][pi] for pi in range(len(dealt))] for g in range(len(names))]


def new_tournament_cup(st, entry, difficulty, mine_teamid, seed=None):
    """A tournament instance, cup-shaped so CupProxy/CupMainScreen render it: one round-robin
    phase of len(format.group_names) groups (format.group_rounds rounds each, one team of every
    pot in each group), then the knock-out bracket built from format.knockout_pairs. The instance
    keeps its own copy of the format, so editing the config cannot change a cup in progress."""
    fmt = dict(entry.get("format") or {})
    names = fmt.get("group_names") or "ABCDEFGH"
    size = int(fmt.get("group_size") or len(entry.get("pots") or ()))
    rounds = int(fmt.get("group_rounds") or max(1, size - 1))
    teams = tournament_draw(entry, mine_teamid, seed)
    groups = [{"name": f"Grup {names[g]}", "id": names[g],
               "teams": [hc.cup_team(st, t[0], mine_teamid) for t in teams[g]],
               "rounds": [{"shuffle": r == 0, "pairs": [{"legs": [{"homeidx": h, "awayidx": a}]}
                                                       for h, a in hc.league_round(list(range(size)), r)]}
                          for r in range(rounds)]} for g in range(len(teams))]
    cupid = f"{int(time.time()):08x}{os.urandom(8).hex()}"
    cup = {"_id": cupid, "metaid": entry["metaid"], "name": entry["name"], "type": "wcpve",
           "nation": mine_teamid, "difficulty": difficulty,
           "format": fmt, "current": {"phase": 0, "round": 0, "leg": 0},
           "phases": [{"system": "round-robin", "name": "PVECUP_PROG_LEAGUE", "maxrounds": rounds, "groups": groups}],
           "coaches": [{"_id": hc.coach_id(hc._default_slot(st)), "state": "playing"}],
           "teams": [hc.cup_team(st, t[0], mine_teamid) for grp in teams for t in grp]}
    hc.cup_refresh_stats(cup)
    return cup


def tournament_group_stats(g):
    """{team index: (points, goal difference, goals)} of one group: the numbers group_table()
    sorts on, kept because the best third-placed teams are compared across groups."""
    t = {i: [0, 0, 0] for i in range(len(g["teams"]))}
    for rnd in g["rounds"]:
        for pair in rnd["pairs"]:
            leg = pair["legs"][0]
            if "score" not in leg:
                continue
            for i, f, a in ((leg["homeidx"], leg["score"]["home"], leg["score"]["away"]),
                            (leg["awayidx"], leg["score"]["away"], leg["score"]["home"])):
                t[i][0] += 3 if f > a else 1 if f == a else 0
                t[i][1] += f - a
                t[i][2] += f
    return {i: tuple(v) for i, v in t.items()}


def tournament_qualifiers(cup, fmt):
    """(qualifiers, pairs). qualifiers = [A1, A2, B1, B2, ...] in group order; when
    format.best_thirds > 0 the best third-placed teams follow (points / goal difference / goals),
    so they get the indices after the per-group ones. pairs are indices into that list:
    format.knockout_pairs, or sequential pairs when the config does not say."""
    per = int(fmt.get("qualify_per_group") or 2)
    groups = cup["phases"][0]["groups"]
    qual, thirds = [], []
    for g in groups:
        stats = tournament_group_stats(g)
        order = sorted(stats, key=lambda i: tuple(-v for v in stats[i]))
        qual += [g["teams"][order[k]] for k in range(per)]
        if len(order) > per:
            thirds.append((stats[order[per]], g["teams"][order[per]]))
    best = int(fmt.get("best_thirds") or 0)
    if best:
        qual += [team for _, team in sorted(thirds, key=lambda s: tuple(-v for v in s[0]))[:best]]
    pairs = fmt.get("knockout_pairs") or [[i, i + 1] for i in range(0, len(qual), 2)]
    return qual, [list(p) for p in pairs]


def tournament_knockout(qual, pairs):
    """Single-elimination bracket laid out from the pairs: len(pairs) -> /2 -> ... -> 1 rounds
    (8/4/2/1 for a 16-team bracket). Legs are left empty; tournament_play_round fills each round
    from the previous winners.

    The last round holds exactly ONE pair. CupMainStandingTournamentTabPage.shuffleNodes() walks
    the rounds from the last one backwards and, for every pair j of round k, reads
    round[k-1].pairs[2j] and [2j+1] with no bounds check — a second pair in the final round (the
    third-place match this used to append) therefore indexes past the semi-final round and the
    淘汰赛 tab dies with "TypeError: Error #1010" before anything is drawn. The client has no
    third-place node at all (no THIRD/3RD string in CupAssets), so format.third_place_match is
    gone from the config; a bracket of 8 pairs needs 8/4/2/1 rounds, nothing more."""
    n, sizes = len(pairs), []
    while n >= 1:
        sizes.append(n)
        n //= 2
    rounds = [{"shuffle": i == 0, "pairs": [{"legs": [{}]} for _ in range(k)]}
              for i, k in enumerate(sizes)]
    for k, (i, j) in enumerate(pairs):
        rounds[0]["pairs"][k]["legs"][0] = {"homeidx": i, "awayidx": j}
    return {"system": "knock-out", "name": "MODENAME_MULTI_TOUNAMENT", "maxrounds": len(rounds),
            "groups": [{"teams": qual, "rounds": rounds}]}


def tournament_play_round(st, cup, mine=None):
    """Same contract as cup_play_round but generic in the format: the group stage advances through
    its rounds then builds the bracket from tournament_qualifiers(); knock-out draws are settled by
    penalties, and the two semi-final losers go into the third-place pair."""
    entry = tournament_entry(cup.get("metaid")) or {}
    fmt = cup.get("format") or entry.get("format") or {}
    cur = cup["current"]; phase = cup["phases"][cur["phase"]]; ko = phase["system"] == "knock-out"
    rng = random.Random()
    for g in phase["groups"]:
        for pair in g["rounds"][cur["round"]]["pairs"]:
            leg = pair["legs"][cur["leg"]]
            if "score" in leg or leg.get("homeidx") is None:
                continue
            h, a = g["teams"][leg["homeidx"]], g["teams"][leg["awayidx"]]
            played = mine is not None and (h["coach"] or a["coach"])
            if played:
                gh, ga = mine[:2]
            else:
                gh, ga = hc.sim_score(rng, tournament_strength(entry, h["teamid"]), tournament_strength(entry, a["teamid"]))
            leg["score"] = {"home": gh, "away": ga}
            if h["coach"] or a["coach"]:
                hc.record_match(st, "cup", *((gh, ga) if h["coach"] else (ga, gh)))
            if ko and gh == ga and played and len(mine) > 3 and mine[2] != mine[3]:
                leg["shootout"] = {"home": mine[2], "away": mine[3]}
            elif ko and gh == ga:
                ph = rng.randint(3, 5); pa = ph + rng.choice((-1, 1)) if ph < 5 else ph - 1
                leg["shootout"] = {"home": ph, "away": pa}
    me_state = cup["coaches"][0]
    if not ko:
        if cur["round"] + 1 < len(phase["groups"][0]["rounds"]):
            cur["round"] += 1
            return
        qual, pairs = tournament_qualifiers(cup, fmt)
        cup["phases"].append(tournament_knockout(qual, pairs))
        cup.update(current={"phase": cur["phase"] + 1, "round": 0, "leg": 0})
        if not any(t["coach"] for t in qual):
            me_state["state"] = "eliminated"
        return
    g = phase["groups"][0]; rounds = g["rounds"]
    results = []
    for pair in rounds[cur["round"]]["pairs"]:
        leg = pair["legs"][0]; s = leg.get("shootout") or leg.get("score")
        if s is None or leg.get("homeidx") is None:
            results.append(None)
            continue
        win = leg["homeidx"] if s["home"] > s["away"] else leg["awayidx"]
        lose = leg["awayidx"] if s["home"] > s["away"] else leg["homeidx"]
        results.append((win, lose))
    played = [r for r in results if r]
    winners = [w for w, _ in played]
    my_idx = next((i for i, t in enumerate(g["teams"]) if t["coach"]), None)
    if my_idx is not None and me_state["state"] == "playing" and my_idx not in winners and \
            any(my_idx in (p["legs"][0].get("homeidx"), p["legs"][0].get("awayidx"))
                for p in rounds[cur["round"]]["pairs"]):
        me_state["state"] = "eliminated"
    if cur["round"] < len(rounds) - 2:  # before the semis: fill the next round from the winners
        for k, pair in enumerate(rounds[cur["round"] + 1]["pairs"]):
            pair["legs"][0] = {"homeidx": winners[2 * k], "awayidx": winners[2 * k + 1]}
        cur["round"] += 1
    elif cur["round"] == len(rounds) - 2:  # semis done: the final (no third-place match, see
        fin = rounds[-1]["pairs"][0]["legs"][0]  # tournament_knockout: the client cannot draw it)
        fin.update(homeidx=played[0][0], awayidx=played[1][0])
        cur["round"] = len(rounds) - 1
    else:  # final (and third place) played
        cup["finished"] = True
        if me_state["state"] == "playing":
            fin = rounds[-1]["pairs"][0]["legs"][0]
            s = fin.get("shootout") or fin["score"]
            winner = fin["homeidx"] if s["home"] > s["away"] else fin["awayidx"]
            me_state["state"] = "won" if my_idx == winner else "eliminated"


def stored_lineup(squad):
    """A squad in the compact, restart-proof shape a cup keeps: every slot by SPID, not by the
    card instance id. The generated tournament squads use str(spid) as their instance id, but an
    id that is stable across a state reload (and a DB re-import) is what makes the XI survive
    into the next round."""
    return {"positions": [{"spid": str(p.get("spid") or p.get("id")), "pos": int(p.get("pos", hc.POS_SUB)),
                           **({"starting": p["starting"]} if p.get("starting") else {})}
                          for p in squad.get("positions") or ()],
            "formation": squad.get("formation"),
            "captain": str(squad.get("captain") or ""),
            "kickers": [str(k or "") for k in (squad.get("kickers") or ())][:hc.KICKERS]}


def apply_cup_lineup(team, lineup):
    """Put a stored lineup (see stored_lineup) on a generated squad; False when the shape no
    longer fits (fewer than 11 known starters), which leaves the default XI in place.

    The clamp is deliberate: the card sources of a cup can change between rounds (a config edit,
    a team that fell back to its plain roster), and dropping one unknown card must not field a
    10-man side."""
    if not lineup or not team:
        return False
    squad = team.get("squad") or {}
    players = {str(p.get("spid")): p for p in team.get("players") or ()}
    positions, seen = [], set()
    for p in lineup.get("positions") or ():
        spid = str(p.get("spid") or "")
        pos = int(p.get("pos", hc.POS_SUB))
        if spid not in players or spid in seen or pos > hc.POS_SUB:
            continue
        seen.add(spid)
        positions.append({"spid": spid, "pos": pos,
                          **({"starting": p["starting"]} if p.get("starting") else {})})
    if sum(1 for p in positions if p["pos"] < hc.POS_SUB) != 11:
        return False
    squad["positions"] = positions
    if lineup.get("formation"):
        squad["formation"] = lineup["formation"]
    if str(lineup.get("captain") or "") in players:
        squad["captain"] = str(lineup["captain"])
    kickers = [str(k) for k in lineup.get("kickers") or () if str(k or "") in players][:hc.KICKERS]
    if kickers:
        squad["kickers"] = kickers
    return True


def cup_lineup_side(cup, req):
    """My side of a tournament match rebuilt from the cup's own squad + the squad the client just
    sent: the pre-match screen's swap / formation changes land on the CUP, not on the coach's club
    (ep_match_squad used to write them into st["teams"], so a World Cup side came back as the
    club's default 4-4-2). Returns the owned team, or None when the request does not fit."""
    entry = tournament_entry(cup.get("metaid"))
    base = copy.deepcopy(tournament_squad(entry, cup.get("nation")))
    if not base:
        return None
    # +N as `ep_cup_match` does it: this path rebuilds the side from the raw squad (grade 0), so
    # without the grade every pre-match squad PUT downgraded my XI back to +0 while the AI kept
    # its +1 (2026-11-26).
    team = hc.owned_team(hc.set_card_grade(base, (entry or {}).get("card_grade", hc.CUP_AI_GRADE)))
    apply_cup_lineup(team, cup.get("lineup"))
    if not hc.apply_squad(team, req):
        return None
    cup["lineup"] = stored_lineup(team["squad"])  # the next round starts from this XI
    return team
