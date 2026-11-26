"""The /cup/* endpoints: the surface every cup screen goes through.

Both kinds of cup are served here, and which engine plays a round is decided in this module:

    club cups    the 16-club draw that stays in http_config.py (new_cup, cup_play_round,
                 cup_refresh_stats -- upstream code, kept where upstream wrote it)
    tournaments  the World Cup / Euro, cup type "wcpve", engine in modules/tournaments.py

The cup instance is the same shape either way, so the client draws both with CupFrontScreen /
CupMainScreen and the same CupProxy calls. What this layer adds on top of the upstream handlers is
the seam between them: /cup/new resumes a running cup instead of starting an empty one, leaving and
creating push /coach/me back so CupProxy.cupPlayList stays in sync, a forfeit counts 0-3, and every
reply carries the cup as a `resources` entry (see docs/世界杯.md).
"""
import copy
import json
import time

from modules import hc  # http_config, resolved lazily (see modules/__init__.py)
from modules.tournaments import (apply_cup_lineup, cup_or_tournament_meta, new_tournament_cup,
                                 tournament_config, tournament_entry, tournament_meta,
                                 tournament_play_round, tournament_squad)


def ep_cup_list(query, body):
    if "type=wcpve" in query:  # the native WCProxy list: only a client in WC mode asks for it
        return hc._json([tournament_meta(e) for e in tournament_config()])
    return hc._json(hc.CUPS + [tournament_meta(e, "pve")
                               for e in tournament_config() if e.get("listed", True)])


# --- Cup resources / resume ------------------------------------------------- #


def cup_resources(cup):
    """The cup with its meta's trophy and skin (issue #36: CupMainScreen draws
    trophies/t<currentCup.trophy>.png, and cups saved without them asked for tundefined.png) and
    teamselect (CupMainScreen/CupSquadSettingHandler ask currentCup.teamselect whether this is a
    licensed side: it hides the squad screen's contract-extension button and the OVR rank)."""
    uri = f"/cup/{cup['_id']}"
    meta = cup_or_tournament_meta(cup.get("metaid")) or {}
    return [{"resource": uri, "uri": uri, "body": {"trophy": meta.get("trophy"), "skin": meta.get("skin"),
                                                  "teamselect": meta.get("teamselect"), **cup}}]


def ongoing_cup(st, metaid):
    """The cup of this meta that was not left yet, or None.

    POST /cup/new is what the client sends right after the team picker, and it sends it every
    time: the front screen knows a cup is already running only through CupProxy.cupPlayList, and
    that list is filled from /coach/me's `cups` and refreshed only when that resource changes --
    a cup created in this session stayed invisible to the picker until the next login, so leaving
    the cup menu and opening it again re-picked a team and started a second, empty cup while the
    one just played sat in the state (2026-11-25). Handing the running cup back instead of a new
    one keeps the progress, and the reply carries /coach/me so the list is right from then on.

    First match wins: /coach/me lists the cups in insertion order and the front screen resumes the
    first entry whose metaid matches (ArrayUtil.findIndexOf), so the same cup must win here."""
    for cup in (st.get("cups") or {}).values():
        if int(cup.get("metaid") or 0) == int(metaid or 0):
            return cup
    return None


def ep_cup_new(query, body):
    """POST /cup/new {metaid, difficulty, teamid, lineup}. For a tournament meta the client
    sends `teamid` = the national team picked in the 3-wheel picker, not the club.

    A cup already running for that metaid is returned as it is (see ongoing_cup); the original
    server answered "already this cup is in progress" here, which the client turns into an error
    popup -- resuming is what a private server wants."""
    req = json.loads(body or b"{}")
    metaid = int(req.get("metaid") or 0)
    meta = cup_or_tournament_meta(metaid)
    if meta is None:
        return hc._json({"result": False, "reason": "invalid metaid"})
    st = hc._state()
    cup = ongoing_cup(st, metaid)
    if cup is None:
        entry = tournament_entry(metaid)
        if entry is not None:
            nation = int(req.get("teamid") or 0) or hc.my_teamid(st)
            cup = new_tournament_cup(st, entry, int(req.get("difficulty") or 0), nation)
        else:
            cup = hc.new_cup(st, meta, int(req.get("difficulty") or 0))
        st.setdefault("cups", {})[cup["_id"]] = cup
        r = hc.coach_record(hc.my_coach(st), "cup")
        r["totalchallenges"] = r.get("totalchallenges", 0) + 1
        hc._save(st)
    return hc._json({"result": True, "resources":
                     cup_resources(cup) + hc.coach_me_resources(st, hc._default_slot(st))})


def cup_step(cupid, fn):
    st = hc._state(); cup = (st.get("cups") or {}).get(cupid)
    if cup is None:
        return hc._json({"result": False, "reason": "not exist cupid"})
    out = fn(st, cup) or {}
    hc._save(st)
    return hc._json(dict({"result": True, "resources": cup_resources(cup)}, **out))


def ep_cup_resume(cupid, query, body):
    return cup_step(cupid, lambda st, cup: None)


def ep_cup_next(cupid, query, body):
    """PUT /cup/<id>/next: play out the current round (my result from the match just played,
    if any) and advance (CupController.onMatchEndReceived -> ON_CUP_SCHEDULE_NEXT_ROUND)."""
    def step(st, cup):
        if cup.get("finished"):
            return
        play = tournament_play_round if cup.get("type") == "wcpve" else hc.cup_play_round
        play(st, cup, cup.pop("_mine", None))
        hc.cup_refresh_stats(cup)
    return cup_step(cupid, step)


def cup_reward_ep(cup):
    """EP for an ended cup: the meta's prize for the champion, a share of it by the round
    reached otherwise (group exit 10%, QF 20%, SF 35%, final 50%); 0 while still playing."""
    state = cup["coaches"][0]["state"]
    meta = cup_or_tournament_meta(cup["metaid"]) or hc.CUPS[0]
    prize = meta["bestrewards"][-1][0]["count"]
    if state == "won":
        return prize
    if state != "eliminated":
        return 0
    ko = cup["phases"][1]["groups"][0] if len(cup["phases"]) > 1 else None
    mine = next((i for i, t in enumerate(ko["teams"]) if t["coach"]), None) if ko else None
    if mine is None:
        return prize // 10
    played = sum(1 for r in ko["rounds"] for p in r["pairs"]
                 if mine in (p["legs"][0].get("homeidx"), p["legs"][0].get("awayidx")) and "score" in p["legs"][0])
    return prize * (20, 35, 50)[max(0, min(played, 3) - 1)] // 100


def ep_cup_leave(cupid, query, body):
    """PUT /cup/<id>/leave: give up, or collect an ended cup's reward. CupReward*Scene.onItemExpand
    reads body.rewards[]: itemid 1 = EP (count), anything else is an item card."""
    st = hc._state(); cup = (st.get("cups") or {}).pop(cupid, None)
    ep = cup_reward_ep(cup) if cup else 0
    hc.add_ep(st, ep)
    if cup:  # Info Manager's cup record: given up, won (by AI level), prize money and the best
        r, state = hc.coach_record(hc.my_coach(st), "cup"), cup["coaches"][0]["state"]
        r["abandonments"] = r.get("abandonments", 0) + (state == "playing")
        if state == "won":
            r["totalwins"] = r.get("totalwins", 0) + 1
            wins = r.setdefault("cupwins", [0] * hc.CUP_LEVELS)
            wins[min(hc.CUP_LEVELS - 1, max(0, int(cup.get("difficulty", 0))))] += 1
        r["totalprize"] = r.get("totalprize", 0) + ep
        if ep > (r.get("best") or {}).get("prize", 0):
            r["best"] = {"prize": ep, "metaid": cup["metaid"], "date": hc.w3c(time.time())}
    hc._save(st)
    # /coach/me carries the remaining cups: feeding it makes CoachController raise onCupsChanged,
    # so CupProxy.cupPlayList drops the cup the client just left instead of offering a resume.
    return hc._json({"result": True, "rewards": [{"itemid": 1, "count": ep}] if ep else [],
                     "resources": hc.coach_me_resources(st, hc._default_slot(st))})


def ep_cup_match(cupid, query, body):
    """POST /cup/<id>/match -> my fixture of the current round as a /match/<id> resource."""
    st = hc._state(); cup = (st.get("cups") or {}).get(cupid)
    g, leg = hc.cup_my_pair(cup) if cup else (None, None)
    if leg is None or "score" in leg:
        return hc._json({"result": False, "reason": "already played match"})
    me = hc.coach_me(st, hc._default_slot(st))
    tournament = cup.get("type") == "wcpve"
    # In a tournament cup the player coaches a national team, not his club: the side is
    # cup["nation"] and its roster is the nation's, so home/away/opponent all key
    # off that id instead of the coach's club teamid.
    teamid = cup["nation"] if tournament else me["teamid"]
    home = g["teams"][leg["homeidx"]]["teamid"]; away = g["teams"][leg["awayidx"]]["teamid"]
    opp = away if home == teamid else home
    user = {"id": me["id"], "name": me["name"], "levelinfo": me["levelinfo"]}
    if tournament:
        entry = tournament_entry(cup.get("metaid"))
        # BOTH sides come from the cup's card sources (the event's own card season first), so the
        # opponent really is the squad the draw put in this group; a nation the sources cannot
        # make 11 players for falls back to its normal roster (tournament_roster_team does that).
        #
        # match type is Cup_PvE, NOT WorldCup_PvE: a tournament here is reached through the cup
        # flow (CupFrontScreen -> CupCreationScreen -> CupSquadSettingScreen), and that flow ends on
        # LeagueCupGameSettingScreen. Its onPlaying() branches on the match type and, for a
        # WorldCup_* type, reads the native WC screens' data (WCProxy / the WC player-pack screen),
        # which this flow never loads -- that object is null and it throws AS3 #1009 before
        # StartSingleMatch ("Cannot access a property or method of a null object reference at
        # LeagueCupGameSettingScreen/onPlaying", 2026-11-23). A Cup_PvE match keeps the same cup
        # data and rules (drawMode 2 extra time, cup_pending scoring) without that branch.
        # +N cards: a generated squad carries no grade (= +0), so an AI side would show every card
        # unenhanced. Both ends of a tournament are generated, so both take the event's card_grade.
        grade = (entry or {}).get("card_grade", hc.CUP_AI_GRADE)
        # My XI / formation as left on the pre-match squad screen (cup["lineup"], written by
        # ep_match_squad): a fresh round otherwise starts from the generated 4-4-2 again. A copy,
        # because tournament_squad is cached and shared with the draw and the other rounds.
        mine = copy.deepcopy(tournament_squad(entry, teamid))
        apply_cup_lineup(mine, cup.get("lineup"))
        match = hc.new_match("Cup_PvE",
                             hc.match_side(hc.set_card_grade(mine, grade), teamid, user, hc._tactics(st)[1]),
                             hc.match_side(hc.set_card_grade(tournament_squad(entry, opp), grade), opp))
    else:
        match = hc.new_match("Cup_PvE",
                             hc.match_side(st["teams"][str(hc._default_slot(st))], teamid, user,
                                           hc._tactics(st)[1]),
                             # the AI clubs only; my own cards keep the grades I gave them
                             hc.match_side(hc.set_card_grade(hc.roster_team(opp), hc.CUP_AI_GRADE), opp))
    gs = match["gamesettings"]
    gs["home"] = home == teamid
    gs["kit"] = ["home", "away"] if gs["home"] else ["away", "home"]
    hc.apply_prematch(st, match)
    gs["difficulty"] = cup.get("difficulty", 0)  # AI level 0..4
    if cup["phases"][cup["current"]["phase"]]["system"] == "knock-out":
        # native maps drawMode 1/2/3 to end rules 9/6/7 (0 = a draw stands). 2 plays extra time
        # (FIRST_EXTRA_HALF seen in game); a draw after it is settled by cup_play_round
        gs["drawMode"] = 2
    st["match"] = match
    st["cup_pending"] = {"id": match["id"], "cupid": cupid, "home": home, "away": away}
    hc._save(st)
    return hc._json({"result": True, "resources": [{"uri": f"/match/{match['id']}", "body": match}]})


def ep_cup_record(query, body):
    """GET /cup/record: CupProxy.onCupRecordResourceChanged takes resources[] entries whose
    `resource` is /cup/record and body._id is my coach id."""
    st = hc._state(); coach = hc.my_coach(st)
    r = coach.get("record", {}).get("cup", {})
    return hc._json({"result": True,
                     "resources": [{"resource": "/cup/record", "uri": "/cup/record",
                                    "body": {"_id": coach["_id"], "recordsbymetaid": [],
                                             **{k: r.get(k, 0) for k in ("totalchallenges", "totalwins",
                                                                         "totalprize")}}}]})
