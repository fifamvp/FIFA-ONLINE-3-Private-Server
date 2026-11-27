"""Tournament cups (PvE): the World Cup / European Championship engine.

An event is data: one entry of config/tournaments.json -- metaid, name, the seeding pots, the card
sources a side is built from, and the format. This module turns that entry into a cup-shaped
instance the client already knows how to draw (CupProxy / CupMainScreen), picks both squads, plays
the AI rounds and builds the knock-out bracket.

Why a cup at all: the client has no worldcup button (MainMenuInfoDefines hardcodes league/cup/rtw)
and its competition creation scene cannot be reached, but CupFrontScreen draws any /cup/list tile.
So a tournament goes through the ordinary cup flow -- /cup/list -> the 3-wheel team picker
(SORTNATION / SORTLEAGUE / SORTTEAM) -> POST /cup/new -> /cup/<id>/match, /cup/<id>/next -- and the
player coaches a NATIONAL team. The id travels, not the squad: the client resolves nationalteamid
-> nation from its own static DB, and the squad behind the id is picked by NATIONALITY rather than
nationalteamid, because 547 of the 1040 14W cards carry -1 and the whole of Italy's 2014 set does
(gen_teams.national_code / ._team_filter).

Where the rest lives: the /cup/* endpoints for both cup kinds are in modules/cup_api.py, which
calls in here for a "wcpve" cup; the club cup engine (new_cup, cup_play_round, cup_refresh_stats) is
upstream code and stays in http_config.py, reached through `hc`.

This is a config entry and not a responses/ file on purpose: responses/<path>.json is an HTTP
response override keyed by request path (see from_file in http_config), so a file there would
shadow an endpoint.
"""
import copy
import json
import os
import random
import time
import unicodedata
from contextlib import closing

import gen_teams

from modules import hc  # http_config, resolved lazily (see modules/__init__.py)

TOURNAMENT_CONFIG = ("config", "tournaments.json")


def tournament_config_file():
    """config/tournaments.json, resolved per call: importing this module must not read anything
    off the host (see `hc` in modules/__init__.py)."""
    return os.path.join(hc.HERE, *TOURNAMENT_CONFIG)


# The event's own card sources are part of the event, not of these defaults: 14W is season 77,
# 2018 is another season code, and a config that puts one of them here would field 14W players in
# every future tournament. A cup that says nothing falls back to the plain current pool.
TOURNAMENT_DEFAULTS = {"card_sources": [{"current": True}],
                       # A tournament side is the real 23-man squad: 11 XI + 7 named + 5 reserves
                       # (config/squads.json `reserves`), so squad_size is 23 unless a cup says
                       # otherwise. squad_lines is the shape the server keeps when it fills the
                       # roster itself: at least 4 defenders / 5 midfielders / 1 striker and at
                       # most 3 keepers (gen_teams.compose).
                       "squad_size": gen_teams.ROSTER_SIZE,
                       "squad_lines": {"GK": 1, "DF": 4, "MF": 5, "FW": 1},
                       "min_players": 11, "type": "wcpve",
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


# --- configured squads (config/squads.json) -------------------------------- #
#
# A tournament side is generated: gen_teams picks the best XI of the event's card sources in a
# 4-4-2. This section puts a hand-written lineup on top of it -- formation, XI, bench, shirt
# numbers, captain, corner / free-kick / penalty takers, and optionally the CPU's tactic board.
#
# Why a config file and not responses/: same reason as config/tournaments.json (see the module
# docstring) -- responses/<path>.json shadows a real endpoint.
#
# Shape, one entry per team, shared by every cup with a per-cup override:
#   {"defaults": {...}, "teams": {"<teamid>": {...}},
#    "cups": {"<metaid>": {"teams": {"<teamid>": {...}}}}}
# A player is written as a name (looked up in that team's own `players` map, the 1:1 name ->
# playerid roster of the team), as a playerid (an int), or as {"playerid": N, "season": S} to force
# one card. Keeping the map per team means "R. Rodriguez" in Switzerland's block can only be the
# Swiss one, without a global map having to spell out which Ricardo it means. Names are matched
# folded: nameschi / nameseng full, short or shirt name, no case / spacing / accents (Höwedes =
# Howedes). A name missing from the map falls back to the squad's own pool.
#
# The card a player gets: his card that the squad already has (the pool's first one), else the
# event's own season (14W = 77), else the current pool's season (88 / 300), else his newest licensed
# card whatever season it is -- a 2014 side fields Fred on his 2010 card rather than a hole. A
# configured player the pool cannot offer at all is added to the squad from that season -- that is
# how a lineup names someone outside its nation's pool, or a card of another season on purpose.
#
# The roster is 11 starters + 7 named subs + 5 `reserves` = the real 23, and the configured players
# always make it: the server only fills what the config leaves open (see apply_lineup).
LINEUP_FILE = ("config", "squads.json")
LINEUP_SUBS = 7  # the bench the client expects (owned_team.fill_squad tops it up to 7)
LINEUP_RESERVES = 5  # the rest of a 23-man squad; they are in players, not in squad.positions
_LINEUP_CACHE = {"mtime": None, "data": {}}
_FOLD = str.maketrans({"ø": "o", "Ø": "o", "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ß": "ss",
                       "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe", "þ": "th", "ð": "d"})


def lineup_file():
    """config/squads.json, resolved per call (importing this module reads nothing off the host)."""
    return os.path.join(hc.HERE, *LINEUP_FILE)


def lineup_config():
    """config/squads.json, re-read when its mtime changes: editing a lineup needs no restart, and
    the squad cache is dropped so the next draw / match builds the squads again."""
    try:
        mtime = os.stat(lineup_file()).st_mtime
    except OSError:
        return {}
    if _LINEUP_CACHE["mtime"] != mtime:
        try:
            with open(lineup_file(), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            print("squads.json unavailable:", e, flush=True)
            return {}
        _LINEUP_CACHE.update(mtime=mtime, data=data if isinstance(data, dict) else {})
        _TOUR_SQUADS.clear()
    return _LINEUP_CACHE["data"]


def lineup_entry(entry, teamid):
    """The lineup of one team: defaults <- teams.<id> <- cups.<metaid>.teams.<id>. None when the
    team has no entry anywhere (it then keeps the generated 4-4-2). `players` (the name -> playerid
    roster) is merged key by key like `tactic`'s sub-objects, so a layer can add one name without
    dropping the ones below it."""
    cfg = lineup_config()
    if not cfg:
        return None
    layers = [cfg.get("defaults") or {},
              (cfg.get("teams") or {}).get(str(teamid)) or {},
              (((cfg.get("cups") or {}).get(str((entry or {}).get("metaid"))) or {})
               .get("teams") or {}).get(str(teamid)) or {}]
    out, roster = {}, {}
    for layer in layers:
        out.update({k: v for k, v in layer.items() if k != "players"})
        roster.update(layer.get("players") or {})
    if out or roster:
        out["players"] = roster
    return out or None


def _formation(ref):
    """One entry of the client's own formation table, by formationname ("4-2-3-1(1)") or id."""
    if ref is None or ref == "":
        return None
    forms = hc.formation_table().get("common") or []
    if isinstance(ref, int) or str(ref).isdigit():
        return next((f for f in forms if f["formationid"] == int(ref)), None)
    return next((f for f in forms if f["formationname"] == str(ref)), None)


def _name_key(s):
    """A name folded for matching: no accents, no punctuation, lower case (Höwedes = Howedes)."""
    s = unicodedata.normalize("NFKD", str(s or "").translate(_FOLD))
    return "".join(ch for ch in s.lower() if ch.isalnum())


def lineup_players(cfg):
    """A team entry's own `players` roster, folded name -> playerid. Per team on purpose: "R.
    Rodriguez" cannot be the Swiss and the Colombian at once, and three de Jongs stop being a
    problem. A name missing here falls back to the pool's own name index."""
    return {key: pid for key, pid in ((_name_key(k), v)
                                      for k, v in ((cfg or {}).get("players") or {}).items())
            if key and isinstance(pid, int) and not isinstance(pid, bool)}


def _default_seasons(entry, teamid):
    """The seasons a configured player is looked up in when the squad's own pool has no card for
    him: the event's own season(s) first (14W = 77), then the current pool's. Read off the same
    card sources the squad is built from, so a European Championship asks Euro's cards first."""
    out = []
    for src in tournament_card_sources(entry, teamid):
        season = src.get("season")
        if season is not None and int(season) not in out:
            out.append(int(season))
    if gen_teams.CURRENT_SEASON not in out:
        out.append(gen_teams.CURRENT_SEASON)
    return out


def _licensed_spid(c, spid):
    """The card at one exact spid, licensed only (None when the spid is absent or unlicensed)."""
    return c.execute("""SELECT p.* FROM players p JOIN permid m ON m.spid = p.spid AND m.licensed = 1
                        WHERE p.spid = ?""", (int(spid),)).fetchone()


def _fallback_card(c, playerid, seasons):
    """A player's card from the first of `seasons` that has a licensed one; failing that his newest
    licensed card of any season (a 2014 side fields Fred on his 2010 card rather than a hole, and the
    player the file names stays the player on the pitch). Licensed only: an unlicensed card cannot
    join a squad (see gen_teams)."""
    for season in seasons:
        r = c.execute("""SELECT p.* FROM players p JOIN permid m ON m.spid = p.spid AND m.licensed = 1
                         WHERE p.playerid = ? AND p.spid / 1000000 = ?
                         ORDER BY p.overallrating DESC LIMIT 1""", (playerid, int(season))).fetchone()
        if r is not None:
            return r
    return c.execute("""SELECT p.* FROM players p JOIN permid m ON m.spid = p.spid AND m.licensed = 1
                        WHERE p.playerid = ?
                        ORDER BY p.spid / 1000000 DESC, p.overallrating DESC LIMIT 1""",
                     (playerid,)).fetchone()


def _playerid(ref, names, players=None):
    """A configured player to (playerid, forced season). The team's own roster wins over the pool,
    so a name means the same man everywhere in that block; a bare number under a million is a
    playerid; a number at or above a million is a full spid and is stripped to its playerid (the
    season prefix is NOT a forced season -- pick() tries the exact spid first, then falls back to
    the playerid through the season chain); a dict forces a card."""
    if isinstance(ref, bool) or ref is None:
        return None, None
    if isinstance(ref, dict):
        return _playerid(ref.get("playerid", ref.get("id")), names, players)[0], ref.get("season")
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        n = int(ref)
        # A full spid (52065662, or China's 81182224): keep its playerid; the exact card is tried
        # in pick() and, if absent or unlicensed, the season chain takes over from the playerid.
        return (n % 1_000_000, None) if n >= 1_000_000 else (n, None)
    key = _name_key(ref)
    pid = (players or {}).get(key)
    if pid is not None:
        return pid, None
    pids = names.get(key) or []
    return (pids[0], None) if pids else (None, None)


def _pool(c, team, entry, teamid):
    """(rows by spid, playerid -> spids in pool order, folded name -> playerids, shirt name) of a
    side's cards: every card of its card sources plus the ones already in the squad, so a
    configured player the 30-card cut dropped can still be put into the XI."""
    rows = {}
    nat = gen_teams.national_code(c, teamid)
    for r in gen_teams.squad_rows(c, teamid, tournament_card_sources(entry, teamid),
                                  int((entry or {}).get("squad_size") or gen_teams.ROSTER_SIZE), nat):
        rows.setdefault(str(r["spid"]), r)
    extra = [int(p["spid"]) for p in team.get("players") or () if str(p["spid"]) not in rows]
    if extra:
        q = ",".join("?" * len(extra))
        for r in c.execute(f"""SELECT spid, playerid, preferredposition1, attackingworkrate,
                                      defensiveworkrate, overallrating
                               FROM players WHERE spid IN ({q})""", extra):
            rows.setdefault(str(r["spid"]), r)  # keyed by str like every other spid in here
    pids = sorted({r["playerid"] for r in rows.values()})
    names_of = {}
    if pids:
        q = ",".join("?" * len(pids))
        for table in ("nameschi", "nameseng"):
            for r in c.execute(f"""SELECT playerid, full, short, jersey, full_eng, short_eng
                                   FROM {table} WHERE playerid IN ({q})""", pids):
                names_of.setdefault(r["playerid"], []).extend(
                    n for n in (r["full"], r["short"], r["jersey"], r["full_eng"], r["short_eng"]) if n)
    pid_spids, names, jersey = {}, {}, {}
    for spid, r in rows.items():  # dict order = card source order (season 77 before the current pool)
        pid_spids.setdefault(r["playerid"], []).append(spid)
        for n in names_of.get(r["playerid"], ()):
            key = _name_key(n)
            if key:
                names.setdefault(key, [])
                if r["playerid"] not in names[key]:
                    names[key].append(r["playerid"])
    for pid, spids in pid_spids.items():  # the shirt name, for a card added to the squad
        name = next((n for n in names_of.get(pid, ()) if n and " " not in n), "")
        for spid in spids:
            jersey[spid] = name
    return rows, pid_spids, names, jersey


def _best(rows, used, pos):
    """The best unused card for a slot, on the line that slot belongs to (a missing left back is
    replaced by another defender, not by the best striker), scored like gen_teams does it: rating
    at the slot plus FIT_BONUS for a natural position. A squad with no keeper left still gets a
    body in goal rather than a hole."""
    free = {s: r for s, r in rows.items() if s not in used}
    line = gen_teams.player_line(pos)
    tiers = ({s: r for s, r in free.items() if (r["preferredposition1"] == 0) == (pos == 0)
              and gen_teams.player_line(r["preferredposition1"]) == line},
             {s: r for s, r in free.items() if (r["preferredposition1"] == 0) == (pos == 0)}, free)
    for pool in tiers:
        if pool:
            return max(pool, key=lambda s: (
                gen_teams.slot_rating(pool[s], pos)
                + gen_teams.FIT_BONUS * (pool[s]["preferredposition1"] in gen_teams.FITS.get(pos, ())),
                pool[s]["overallrating"]))
    return None


def _best_sub(rows, used):
    """The best unused card for the bench, by the rating the client shows (any position)."""
    free = [(r["overallrating"], str(s)) for s, r in rows.items() if s not in used]
    return max(free)[1] if free else None


def _line_counts(rows, spids):
    """{line: count} over the given spids, so a filler respects the squad's shape (see
    config/tournaments.json `squad_lines`: at most MAX_GK keepers, a minimum per outfield line)."""
    out = {}
    for s in spids:
        r = rows.get(str(s))
        if r is None:
            continue
        ln = ("GK" if r["preferredposition1"] == 0
              else gen_teams.player_line(r["preferredposition1"]))
        out[ln] = out.get(ln, 0) + 1
    return out


def _best_sub_shaped(rows, used, lines):
    """_best_sub, but it first tops up any line below its floor (the server filling the 23-man
    roster must not drop a side below its required midfield/defence, unless the pool has no one
    left). `lines` is the `squad_lines` dict ({"GK":1,"DF":4,"MF":5,"FW":1})."""
    got = _line_counts(rows, used)
    for ln, floor in sorted((lines or {}).items(), key=lambda kv: -int(kv[1])):
        need = int(floor) - got.get(ln, 0)
        if need <= 0:
            continue
        free = [s for s, r in rows.items()
                if s not in used and gen_teams.player_line(r["preferredposition1"]) == ln]
        if free:  # the best of that line (overallrating, then a fuller row as a tiebreak)
            return max(free, key=lambda s: (rows[s]["overallrating"], s))
    return _best_sub(rows, used)


def apply_lineup(team, entry, teamid):
    """Put the configured formation / XI / bench / numbers / captain / kickers of a team on its
    generated squad (see the section banner). Returns the squad unchanged when the team has no
    entry; numbers, captain and kickers still apply when the entry names no XI."""
    cfg = lineup_entry(entry, teamid)
    if not cfg or not team or not team.get("players"):
        return team
    warned = set()
    with closing(hc._db()) as c:
        rows, pid_spids, names, jersey = _pool(c, team, entry, teamid)
        by_spid = {str(p["spid"]): p for p in team["players"]}
        seasons = _default_seasons(entry, teamid)
        players = lineup_players(cfg)

        def ensure(spid):
            p = by_spid.get(spid)
            if p is None:  # a configured player neither the 30-card cut nor the pool offers
                r = rows[spid]
                p = {"spid": r["spid"], "expandme": "player", "price": r["overallrating"],
                     "jersey": 0, "jerseyname": jersey.get(spid) or gen_teams.jersey_name(c, r["playerid"])}
                team["players"].append(p)
                by_spid[spid] = p
            return p

        def pick(ref, used=None):
            """A configured player to a spid of this side: a full spid in the config is tried as
            that exact card first (licensed), then the card the pool already has, else the event's
            season, else the current season, else his newest licensed card (see _fallback_card),
            else None."""
            pid, season = _playerid(ref, names, players)
            spid = None
            if pid is not None:
                if isinstance(ref, (int, str)) and not isinstance(ref, bool) \
                        and str(ref).isdigit() and int(ref) >= 1_000_000:
                    r = _licensed_spid(c, int(ref))  # the exact spid, licensed only
                    if r is not None:
                        spid = str(r["spid"])
                        if spid not in rows:
                            rows[spid] = r
                            pid_spids.setdefault(pid, []).append(spid)
                            jersey.setdefault(spid, gen_teams.jersey_name(c, pid))
                if spid is None and season is None:
                    spid = next(iter(pid_spids.get(pid) or ()), None)
                if spid is None:  # not in this side's pool (or another season was forced)
                    r = _fallback_card(c, pid, [int(season)] if season else seasons)
                    if r is not None:
                        spid = str(r["spid"])
                        if spid not in rows:
                            rows[spid] = r
                            pid_spids.setdefault(pid, []).append(spid)
                            jersey.setdefault(spid, gen_teams.jersey_name(c, pid))
            if spid is None:
                if str(ref) not in warned:
                    warned.add(str(ref))
                    print(f"squads.json: {teamid}: no card for {ref!r}", flush=True)
            elif used is not None and spid in used:
                return None
            return spid

        form, starters = _formation(cfg.get("formation")), cfg.get("starters") or []
        roster = []
        if form and starters:
            # Two passes: every name first, then the holes. A single pass let the substitute for one
            # missing card steal a player another slot names (Djeugoue out -> Nkoulou pushed to right
            # back, Chedjou to Nkoulou's place, ...), which shifted half the XI.
            used, xi = set(), [None] * len(form["positions"])
            for i in range(len(xi)):
                spid = pick(starters[i]) if i < len(starters) else None
                if spid and spid not in used:
                    used.add(spid)
                    xi[i] = spid
            for i, pos in enumerate(form["positions"]):
                if xi[i] is None:  # no card for that name: the best of the pool on that line takes it
                    xi[i] = _best(rows, used, pos)
                    used.add(xi[i])
            if any(s is None for s in xi):
                print(f"squads.json: {teamid}: cannot fill {form['formationname']}", flush=True)
                return team
            bench = []
            for ref in cfg.get("subs") or ():
                spid = pick(ref, used)
                if spid and len(bench) < LINEUP_SUBS:
                    used.add(spid)
                    bench.append(spid)
            while len(bench) < LINEUP_SUBS:
                spid = _best_sub(rows, used)
                if spid is None:
                    break
                used.add(spid)
                bench.append(spid)
            positions = []
            for i, spid in enumerate(xi):
                ensure(spid)
                r = rows[spid]
                positions.append({"spid": spid, "pos": form["positions"][i],
                                  "starting": [gen_teams.workrate(r, "attackingworkrate"),
                                               gen_teams.workrate(r, "defensiveworkrate"),
                                               form["offsets"][i]["x"], form["offsets"][i]["y"]]})
            for spid in bench:
                ensure(spid)
                positions.append({"spid": spid, "pos": hc.POS_SUB})
            team["squad"] = dict(team.get("squad") or {}, positions=positions, formation=form["formationid"])
            team["formations"] = [form]  # the client's formation list of this side, not gen_teams' fake 4-4-2
            # The rest of the 23: `reserves` first, then the best of the pool, so a roster is a full
            # squad even when the config names fewer. A configured player always makes the roster;
            # the server only fills what the config leaves open.
            reserves = []
            for ref in cfg.get("reserves") or ():
                if len(reserves) >= LINEUP_RESERVES:
                    break
                spid = pick(ref, used)
                if spid:
                    used.add(spid)
                    reserves.append(spid)
            roster = [s for s in xi + bench + reserves if s]
            size = int((entry or {}).get("squad_size") or gen_teams.ROSTER_SIZE)
            shape = (entry or {}).get("squad_lines")
            while len(roster) < size:
                spid = _best_sub_shaped(rows, used, shape)
                if spid is None:
                    break
                used.add(spid)
                roster.append(spid)
            team["players"] = [ensure(spid) for spid in roster]
        sq = team["squad"]
        # Shirt numbers: the configured ones first, then the lowest free 1..99 for whoever is left.
        # gen_teams numbers its own XI 1..11 and the bench 12+, so a configured XI next to it
        # collided (two 7s) unless every single slot were configured.
        explicit = {}
        for ref, n in (cfg.get("numbers") or {}).items():
            spid = pick(ref)
            if spid is None:
                continue
            if spid in explicit or not isinstance(n, int) or not 0 < n < 100:
                print(f"squads.json: {teamid}: bad number {n!r} for {ref!r}", flush=True)
                continue
            explicit[spid] = n
        used, spare = set(), []
        for spid in (roster or [str(p["spid"]) for p in sq.get("positions") or ()]):
            n = explicit.get(spid)
            if n is not None and n not in used:
                used.add(n)
                if spid in by_spid:
                    by_spid[spid]["jersey"] = n
            else:
                spare.append(spid)
        free = [n for n in range(1, 100) if n not in used]
        # the top of 1..23 first: a filler for a missing striker must not end up wearing 1
        free = sorted((n for n in free if n <= 23), reverse=True) + [n for n in free if n > 23]
        for spid, n in zip(spare, free):
            if spid in by_spid:
                by_spid[spid]["jersey"] = n
        xi_ids = {str(p["spid"]) for p in sq.get("positions") or () if p.get("pos", hc.POS_SUB) < hc.POS_SUB}
        cap = pick(cfg["captain"]) if cfg.get("captain") else None
        if cap in xi_ids:
            sq["captain"] = cap
        elif cfg.get("captain"):
            print(f"squads.json: {teamid}: captain {cfg['captain']!r} is not a starter", flush=True)
        kick = [pick((cfg.get("kickers") or {}).get(k)) if (cfg.get("kickers") or {}).get(k) else None
                for k in ("leftcorner", "rightcorner", "freekick", "penalty", "freekick2")]
        kick = [k for k in kick if k in xi_ids]  # owned_team drops a taker who is not in the XI
        if kick:
            sq["kickers"] = (kick + [""] * hc.KICKERS)[:hc.KICKERS]
    return team


def lineup_tactic(entry, teamid):
    """The CPU side's tactic board: hc.default_tactic(0) with the entry's `tactic` merged in, one
    slider at a time (bus / cc / def are dicts). An entry without one keeps the default board."""
    cfg = lineup_entry(entry, teamid) or {}
    t = hc.default_tactic(0)
    for k, v in (cfg.get("tactic") or {}).items():
        t[k] = {**t[k], **v} if isinstance(v, dict) and isinstance(t.get(k), dict) else v
    return hc.fix_radios(t)


def tournament_roster(teamid, entry=None):
    """{summary, team} of a tournament side, or None when even the sources cannot make 11 players
    for it: the configured card sources, so both my side and the opponents field the real players
    of that competition instead of the best cards this DB happens to have for that nation. Picked
    by nationality (see gen_teams._team_filter), so a nation whose cards lost nationalteamid still
    gets its full set. Its formation / XI / bench come from config/squads.json when the team has an
    entry (apply_lineup), the generated 4-4-2 otherwise."""
    with closing(hc._db()) as c:
        team = gen_teams.team(c, teamid, tournament_card_sources(entry, teamid), national=True,
                              by_nation=True,
                              min_players=int((entry or {}).get("squad_size") or gen_teams.ROSTER_SIZE),
                              min_lines=(entry or {}).get("squad_lines"),
                              squad_size=int((entry or {}).get("squad_size") or gen_teams.ROSTER_SIZE))
    if team:
        apply_lineup(team["team"], entry, teamid)
    return team


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
