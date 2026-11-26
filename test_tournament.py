"""Self-check for the tournament engine (modules/tournaments.py) and the /cup/* endpoints it drives.

    python test_tournament.py

No client, no server process: the engine is called directly, the endpoints through route()/
ENDPOINTS against a temporary state.json. Squads come from gamedata/fifadb.sqlite the same way
the server builds them, so the draw and the seat of every card grade are the real ones.

The checks are grouped by what they protect:

    config       config/tournaments.json is read with defaults merged, disabled cups skipped and
                 no restart needed; the shipped file cannot crash the draw or the bracket
    draw         one team of every pot per group, the player's own nation in its pot, a nation
                 outside the pots replacing a pot-4 side
    bracket      group rounds -> knock-out of 8/4/2/1 rounds, exactly one pair in the final
                 (a second one there killed the 淘汰赛 tab with AS3 #1010), no third-place match
    lineup       the XI / formation left on the pre-match screen survives into the next round,
                 and my cards keep the event's +N grade (they dropped to +0 on every PUT)
    endpoints    /cup/list -> /cup/new -> /cup/<id>/match -> next/leave, with every resources
                 entry carrying `resource` (missing one is the same #1010)
    modules      every hc.<name> a module reaches for exists on http_config, and no module
                 assigns to the proxy (a write there is silently lost)
"""
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import http_config as hc  # noqa: E402

from modules import cup_api, localization, team_picker, tournaments  # noqa: E402


def body(resp):
    return json.loads(resp[1])


def call(path, query="", payload=None):
    """The handler a request path resolves to (ENDPOINTS first, then the regex table)."""
    fn = hc.ENDPOINTS.get(path) or hc.route(path)
    assert fn is not None, path
    return body(fn(query, json.dumps(payload).encode() if payload is not None else b""))


def reset_config():
    tournaments._TOUR_CACHE.update(mtime=None, cups=[])
    tournaments._TOUR_SQUADS.clear()


# --------------------------------------------------------------------------- #
# 1. config: the file, not the code, decides what a cup is
# --------------------------------------------------------------------------- #

def check_config():
    cup_json = {
        "defaults": {"trophy": "9", "quality": 3, "card_grade": 2,
                     "format": {"group_names": "AB", "group_size": 2, "group_rounds": 1}},
        "cups": [
            {"metaid": 90001, "name": "Test Cup", "_comment": "dropped",
             "pots": [{"teams": [[1, "A"], [2, "B"]]}, {"teams": [[3, "C"], [4, "D"]]}],
             "format": {"best_thirds": 5}},
            {"metaid": 90002, "name": "Disabled", "enabled": False},
            {"metaid": 90003, "name": "Pots Only", "joinable_teams": "pots", "card_grade": 0,
             "pots": [{"teams": [[5, "E"]]}, {"teams": [[6, "F"]]}]},
        ],
    }
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "tournaments.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cup_json, f)

    original = tournaments.tournament_config_file
    tournaments.tournament_config_file = lambda: path
    reset_config()
    try:
        cups = tournaments.tournament_config()
        assert [c["name"] for c in cups] == ["Test Cup", "Pots Only"], cups  # enabled: false skipped
        entry, pots_only = cups
        assert "_comment" not in entry  # underscore keys are notes, not fields
        assert entry["card_grade"] == 2 and pots_only["card_grade"] == 0  # per-cup over defaults
        assert entry["trophy"] == "9" and entry["quality"] == 3
        assert entry["format"] == {"group_names": "AB", "group_size": 2, "group_rounds": 1,
                                   "best_thirds": 5}, entry["format"]  # format merges key by key
        assert entry["teamselect"] == "authentic"  # TOURNAMENT_DEFAULTS fills what the file omits

        meta = tournaments.tournament_meta(entry)
        assert meta["type"] == "wcpve" and meta["teamcount_desc"] == 4, meta
        assert meta["joinable"]["allteams"] is True  # joinable_teams unset: the whole team wheel
        assert meta["bestrewards"][0][0]["count"] == 6000000  # reward = champion prize
        meta = tournaments.tournament_meta(pots_only, "pve")
        assert meta["type"] == "pve"  # what CupFrontScreen lists; the instance stays wcpve
        assert meta["joinable"]["allteams"] is False and meta["joinable"]["teamids"] == [5, 6], meta
        assert meta["skin"] == "66"  # client asks trophies/t<trophy>.png + skins/<skin>

        assert tournaments.tournament_teamids() == [1, 2, 3, 4, 5, 6]
        assert tournaments.tournament_entry_of_team(3)["metaid"] == 90001
        assert tournaments.tournament_entry_of_team(241) is None  # a club, not a pot team
        assert tournaments.tournament_entry("abc") is None and tournaments.tournament_entry(999) is None
        assert tournaments.cup_or_tournament_meta(1)["metaid"] == 1  # a club cup from CUPS
        assert tournaments.cup_or_tournament_meta(90001)["metaid"] == 90001
        assert tournaments.cup_or_tournament_meta(424242) is None

        # a config edit needs no restart: the next cup created reads the new file
        cup_json["cups"][0]["name"] = "Test Cup v2"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cup_json, f)
        later = time.time() + 5
        os.utime(path, (later, later))
        assert tournaments.tournament_config()[0]["name"] == "Test Cup v2"
        # ...and the squads cached for the old revision are dropped with it
        assert tournaments._TOUR_SQUADS == {}
    finally:
        tournaments.tournament_config_file = original
        reset_config()


def check_shipped_config():
    """The shipped file must be able to drive a cup as it stands: a bad pot size or a pair index
    past the qualifiers is a TypeError in the client, not a nice error on the tile."""
    reset_config()
    cups = tournaments.tournament_config()
    assert cups, "config/tournaments.json enables no cup"
    club_ids = {m["metaid"] for m in hc.CUPS}
    for e in cups:
        assert int(e["metaid"]) not in club_ids, e["metaid"]  # would shadow a club cup's meta
        fmt = e["format"]
        names = fmt.get("group_names") or "ABCDEFGH"
        pots = [[(int(t[0]), t[1] if len(t) > 1 else "") for t in p.get("teams") or ()]
                for p in e.get("pots") or ()]
        assert len(pots) >= 2, e["name"]
        assert all(len(pot) == len(names) for pot in pots), \
            "%s: every pot needs exactly %d teams" % (e["name"], len(names))
        ids = [t[0] for pot in pots for t in pot]
        assert len(ids) == len(set(ids)), "%s: a team appears in two pots" % e["name"]
        assert not (set(ids) & {t[0] for t in e.get("reserves") or ()}), \
            "%s: a reserve is already in a pot" % e["name"]
        per = int(fmt.get("qualify_per_group") or 2)
        best = int(fmt.get("best_thirds") or 0)
        qual = len(names) * per + best
        pairs = fmt.get("knockout_pairs") or []
        if best:
            # a best-third slot makes the qualifier count odd or non-power-of-two, and the
            # sequential fallback would pair an index past the list: the pairs must be explicit
            assert pairs, "%s: best_thirds needs explicit knockout_pairs" % e["name"]
        if pairs:
            assert len(pairs) * 2 == qual, \
                "%s: %d pairs for %d qualifiers" % (e["name"], len(pairs), qual)
            assert len(pairs) & (len(pairs) - 1) == 0, \
                "%s: %d pairs is not a power of two" % (e["name"], len(pairs))
            assert max(i for p in pairs for i in p) < qual and min(i for p in pairs for i in p) >= 0
        assert e["metaid"] == int(e["metaid"])
        assert tournaments.tournament_teamids(e), e["name"]
        # +N must be a real grade: set_card_grade ignores 0, the client shows 0 as unenhanced
        assert 0 <= int(e.get("card_grade") or 0) <= 10, e["name"]


# --------------------------------------------------------------------------- #
# 2. draw: one team of every pot per group
# --------------------------------------------------------------------------- #

def check_draw():
    entry = tournaments.tournament_config()[0]
    pots = [[int(t[0]) for t in (p.get("teams") or ())] for p in entry["pots"]]
    reserves = {int(t[0]) for t in entry.get("reserves") or ()}
    mine = pots[0][0]
    groups = tournaments.tournament_draw(entry, mine, seed=7)
    assert len(groups) == len(entry["format"]["group_names"]), len(groups)
    assert all(len(g) == len(pots) for g in groups)  # one slot per pot
    flat = [t[0] for g in groups for t in g]
    assert len(flat) == len(set(flat)) == len(pots) * len(groups), "a team was drawn twice"
    for g in groups:
        for pi, team in enumerate(g):
            assert team[0] == mine or team[0] in pots[pi] or team[0] in reserves, \
                "pot %d got %d" % (pi, team[0])
    assert sum(1 for g in groups if any(t[0] == mine for t in g)) == 1  # I am in exactly one group
    mine_pot = next(i for i, pot in enumerate(pots) if mine in pot)
    assert next(g for g in groups if any(t[0] == mine for t in g))[mine_pot][0] == mine

    # a nation the cup does not field did not qualify: it takes a pot-4 slot
    outsider = next(t for t in (1413, 1318, 999999) if t not in flat)
    groups = tournaments.tournament_draw(entry, outsider, seed=11)
    flat = [t[0] for g in groups for t in g]
    assert flat.count(outsider) == 1 and len(flat) == len(set(flat))
    last = len(pots) - 1
    assert next(g for g in groups if any(t[0] == outsider for t in g))[last][0] == outsider


# --------------------------------------------------------------------------- #
# 3. bracket: the group phase feeds a knock-out of powers of two
# --------------------------------------------------------------------------- #

def check_qualifiers():
    """Pure function, no DB: the qualifier order the bracket indexes into."""
    def group(name, results):
        return {"name": "Grup " + name, "id": name,
                "teams": [{"teamid": i + 1} for i in range(4)],
                "rounds": [{"shuffle": False, "pairs": [
                    {"legs": [{"homeidx": h, "awayidx": a, "score": {"home": gh, "away": ga}}]}
                    for h, a, gh, ga in results]}]}

    cup = {"phases": [{"groups": [
        # A: 0 wins twice (6 pts), 1 wins once, 2 and 3 lose out
        group("A", [(0, 1, 2, 0), (0, 2, 1, 0), (0, 3, 1, 0), (1, 2, 2, 0), (1, 3, 1, 1), (2, 3, 0, 3)]),
        group("B", [(0, 1, 0, 1), (0, 2, 0, 1), (0, 3, 0, 4), (1, 2, 1, 2), (1, 3, 0, 0), (2, 3, 0, 1)]),
    ]}]}
    groups = cup["phases"][0]["groups"]
    qual, pairs = tournaments.tournament_qualifiers(cup, {"qualify_per_group": 2, "best_thirds": 1})
    # qualifiers are listed group by group, then the best third-placed sides
    assert [t["teamid"] for t in qual[:2]] == [groups[0]["teams"][i]["teamid"]
                                              for i in hc.group_table(groups[0])[:2]], qual
    assert [t["teamid"] for t in qual[2:4]] == [groups[1]["teams"][i]["teamid"]
                                               for i in hc.group_table(groups[1])[:2]], qual
    assert len(qual) == 5  # 2 + 2 + the best third
    # both thirds have 4 points here; A's has the better goal difference, so it is the one added
    a3, b3 = hc.group_table(groups[0])[2], hc.group_table(groups[1])[2]
    assert qual[4]["teamid"] == groups[0]["teams"][a3]["teamid"], qual[4]
    assert tournaments.tournament_group_stats(groups[0])[a3] > \
        tournaments.tournament_group_stats(groups[1])[b3]
    # no knockout_pairs in the format: sequential pairs over an even qualifier list
    qual4, pairs = tournaments.tournament_qualifiers(cup, {"qualify_per_group": 2, "best_thirds": 0})
    assert len(qual4) == 4 and pairs == [[0, 1], [2, 3]], pairs
    _, pairs = tournaments.tournament_qualifiers(cup, {"qualify_per_group": 2, "best_thirds": 1,
                                                       "knockout_pairs": [[0, 2], [1, 3], [4, 4]]})
    assert pairs == [[0, 2], [1, 3], [4, 4]]  # the config's pairs, as lists


def check_engine():
    """A whole cup through the engine: draw -> group phase -> knock-out -> final."""
    entry = tournaments.tournament_config()[0]
    mine = tournaments.tournament_teamids(entry)[0]
    st = {"coaches": [{"slot": 0, "_id": hc.coach_id(0), "teamid": 241, "record": {}}],
          "defaultcoachid": hc.coach_id(0), "teams": {}}
    cup = tournaments.new_tournament_cup(st, entry, 2, mine, seed=3)

    assert cup["type"] == "wcpve" and cup["metaid"] == entry["metaid"] and cup["nation"] == mine
    assert cup["format"] == entry["format"]  # the instance keeps its own copy of the format
    assert len(cup["teams"]) == len(tournaments.tournament_teamids(entry))
    coached = [t for t in cup["teams"] if t["coach"]]
    assert len(coached) == 1 and coached[0]["teamid"] == mine, coached  # only my side has a coach
    phase = cup["phases"][0]
    assert phase["system"] == "round-robin" and phase["name"] == "PVECUP_PROG_LEAGUE"
    assert len(phase["groups"]) == len(cup["format"]["group_names"])
    rounds = len(phase["groups"][0]["rounds"])
    assert rounds == int(cup["format"]["group_rounds"])
    assert all(len(g["teams"]) == int(cup["format"]["group_size"]) for g in phase["groups"])

    for i in range(rounds):
        tournaments.tournament_play_round(st, cup, (2, 1))
        assert cup["current"]["round"] == i + 1 or len(cup["phases"]) == 2, cup["current"]
    assert len(cup["phases"]) == 2, "the group phase did not build a bracket"
    ko = cup["phases"][1]
    assert ko["system"] == "knock-out" and ko["name"] == "MODENAME_MULTI_TOUNAMENT"
    qual = len(ko["groups"][0]["teams"])
    assert qual == len(cup["format"]["group_names"]) * int(cup["format"]["qualify_per_group"]) \
        + int(cup["format"]["best_thirds"]), qual
    sizes = [len(r["pairs"]) for r in ko["groups"][0]["rounds"]]
    assert sizes == [8, 4, 2, 1], sizes  # 16 teams: R16 -> QF -> SF -> final
    assert sizes[-1] == 1, "the final must hold exactly one pair (AS3 #1010 in the 淘汰赛 tab)"

    played = rounds
    while not cup.get("finished"):
        tournaments.tournament_play_round(st, cup, (2, 1))
        played += 1
        assert played < 40, "the cup never finished"
    assert sizes[-1] == 1
    assert cup["coaches"][0]["state"] in ("won", "eliminated"), cup["coaches"]
    rec = st["coaches"][0]["record"]["cup"]
    # one record per match I actually played: the whole group phase, plus the knock-out rounds
    # reached (an eliminated side stops appearing, so this is a floor, not the call count)
    assert rec["wins"] + rec["draws"] + rec["losses"] >= rounds, rec
    fin = ko["groups"][0]["rounds"][-1]["pairs"][0]["legs"][0]
    assert fin.get("homeidx") is not None and fin.get("awayidx") is not None
    assert "score" in fin


# --------------------------------------------------------------------------- #
# 4. lineup: the pre-match screen's XI survives, at the event's grade
# --------------------------------------------------------------------------- #

def check_lineup():
    entry = tournaments.tournament_config()[0]
    mine = tournaments.tournament_teamids(entry)[0]
    fresh = tournaments.tournament_squad(entry, mine)
    assert fresh and len(fresh["players"]) >= 11, "no squad for the first pot team"

    squad = copy.deepcopy(fresh["squad"])
    squad["formation"] = 63
    squad["positions"][1]["pos"], squad["positions"][2]["pos"] = \
        squad["positions"][2]["pos"], squad["positions"][1]["pos"]
    kept = tournaments.stored_lineup(squad)
    assert kept["formation"] == 63 and kept["kickers"] == [str(k) for k in squad["kickers"]][:hc.KICKERS]
    assert {p["spid"] for p in kept["positions"]} == {str(p["spid"]) for p in squad["positions"]}
    assert all("id" not in p for p in kept["positions"])  # by spid, not by card instance id

    team = copy.deepcopy(fresh)
    assert tournaments.apply_cup_lineup(team, kept) is True
    assert team["squad"]["formation"] == 63
    assert sum(1 for p in team["squad"]["positions"] if p["pos"] < hc.POS_SUB) == 11

    # one unknown card must not field a 10-man side: the whole lineup is refused
    short = copy.deepcopy(kept)
    short["positions"][0]["spid"] = "999999999"
    team = copy.deepcopy(fresh)
    assert tournaments.apply_cup_lineup(team, short) is False
    assert team["squad"]["formation"] == fresh["squad"]["formation"]
    assert tournaments.apply_cup_lineup(team, None) is False

    # the pre-match PUT rebuilds my side from the cup's squad -- at the event's grade
    cup = tournaments.new_tournament_cup(
        {"coaches": [{"slot": 0, "_id": hc.coach_id(0), "teamid": 241, "record": {}}],
         "defaultcoachid": hc.coach_id(0), "teams": {}}, entry, 0, mine, seed=5)
    req = {"squad": {"positions": [{"id": p["spid"], "pos": p["pos"]} for p in kept["positions"]],
                     "formation": kept["formation"]},
           "captain": kept["captain"], "kickers": kept["kickers"]}
    side = tournaments.cup_lineup_side(cup, req)
    assert side is not None
    assert {p["grade"] for p in side["players"]} == {int(entry["card_grade"])}, \
        "my cards dropped to +0 on the pre-match PUT (2026-11-26)"
    assert side["squad"]["formation"] == 63
    assert (cup["lineup"] or {}).get("formation") == 63, "the cup did not keep the XI"
    assert tournaments.cup_lineup_side(cup, {"squad": {"positions": []}}) is None


# --------------------------------------------------------------------------- #
# 5. endpoints: the exact calls the cup screens make
# --------------------------------------------------------------------------- #

def new_state_with_coach(tmp):
    """A created manager (the client's own creation flow), on a temporary state.json."""
    hc.STATE = os.path.join(tmp, "state.json")
    hc._save({"coaches": [], "defaultcoachid": "", "teams": {}})
    spids = [p["spid"] for p in hc.roster(241)["team"]["players"]]
    assert call("/coach/0/new/step/3", payload={"id": 241, "spids": spids})["result"]
    assert call("/coach/0", payload={"id": 241, "coachName": "Tester"})["result"]
    assert call("/coach/0/default")["result"]
    st = hc._state()
    assert hc.my_teamid(st) == 241 and st["teams"].get("0")
    return st


def check_endpoints(tmp):
    new_state_with_coach(tmp)
    entry = tournaments.tournament_config()[0]
    mine = tournaments.tournament_teamids(entry)[0]
    grade = int(entry["card_grade"])

    # /cup/list: the native WC list holds only the tournaments; the front screen also gets CUPS
    assert [m["metaid"] for m in call("/cup/list", "type=wcpve")] == [e["metaid"] for e in
                                                                    tournaments.tournament_config()]
    listed = call("/cup/list")
    assert [m["metaid"] for m in listed][:len(hc.CUPS)] == [m["metaid"] for m in hc.CUPS]
    assert entry["metaid"] in [m["metaid"] for m in listed]

    # /cup/new: a resources entry per cup AND /coach/me; both keys on every entry (#1010 in
    # CupProxy.onCupCreateReceived, which reads .resource to skip /cup)
    reply = call("/cup/new", payload={"metaid": entry["metaid"], "difficulty": 2, "teamid": mine})
    assert reply["result"]
    assert all("resource" in r and "uri" in r for r in reply["resources"]), reply["resources"]
    assert [r["resource"] for r in reply["resources"]][-1] == "/coach/me"
    st = hc._state()
    assert len(st["cups"]) == 1
    cupid = next(iter(st["cups"]))
    assert st["cups"][cupid]["nation"] == mine and st["cups"][cupid]["coaches"][0]["state"] == "playing"
    assert st["coaches"][0]["record"]["cup"]["totalchallenges"] == 1

    # a second /cup/new resumes instead of starting an empty second cup (the picker sent it again)
    again = call("/cup/new", payload={"metaid": entry["metaid"], "teamid": mine})
    assert again["resources"][0]["body"]["_id"] == cupid and len(hc._state()["cups"]) == 1
    assert again["resources"][0]["body"]["teamselect"] == entry["teamselect"]

    # the cup itself (/cup/<id>): trophy and skin come from the meta, the cup from the state
    cup_body = call("/cup/%s" % cupid)
    assert cup_body["_id"] == cupid and cup_body["trophy"] == entry["trophy"] and cup_body["skin"]

    # /cup/<id>/match: both sides from the cup's card sources, both at the event's grade
    match = call("/cup/%s/match" % cupid)["resources"][0]["body"]
    assert match["type"] == "Cup_PvE", match["type"]  # not WorldCup_PvE: that flow throws #1009
    assert match["gamesettings"].get("drawMode") == 0  # group stage: a draw stands
    sides = {k: v["team"] for k, v in match["side"].items()}
    assert {s["teamid"] for s in sides.values()} and mine in {s["teamid"] for s in sides.values()}
    for t in sides.values():
        assert {p["grade"] for p in t["players"]} == {grade}, (t["teamid"], t["players"][:1])
        assert sum(1 for p in t["squad"]["positions"] if p["pos"] < hc.POS_SUB) == 11

    # the pre-match squad screen: a new formation lands on the cup, not on my club, and the next
    # round starts from it -- at +N
    side_key = next(k for k, v in match["side"].items() if v["team"]["teamid"] == mine)
    squad = copy.deepcopy(match["side"][side_key]["team"]["squad"])
    squad["formation"] = 63
    saved = call("/match/%s/side/%s/squad" % (match["id"], side_key),
                 payload={"squad": squad, "captain": squad.get("captain"),
                          "kickers": squad.get("kickers")})
    assert saved["result"], saved
    back = saved["resources"][0]["body"]["side"][side_key]["team"]
    assert back["squad"]["formation"] == 63 and {p["grade"] for p in back["players"]} == {grade}
    # _mine is what ep_match_end leaves for /next (my result of the match just played); forcing
    # 5-0 keeps the rest of this check independent of the simulated scores
    st = hc._state()
    st["cups"][cupid]["_mine"] = [5, 0]
    hc._save(st)
    assert call("/cup/%s/next" % cupid)["result"]
    nxt = call("/cup/%s/match" % cupid)["resources"][0]["body"]
    my_side = next(v["team"] for v in nxt["side"].values() if v["team"]["teamid"] == mine)
    assert my_side["squad"]["formation"] == 63, "the next round lost the formation"
    assert {p["grade"] for p in my_side["players"]} == {grade}

    # through the remaining group rounds, then a knock-out tie plays extra time (drawMode 2).
    # Stop as soon as the bracket exists: letting the engine simulate my R16 could knock me out,
    # and then there is no fixture left to ask for.
    st = hc._state()
    cup = st["cups"][cupid]
    while len(cup["phases"]) == 1:
        tournaments.tournament_play_round(st, cup, (5, 0))  # win every group match: I go through
    hc._save(st)
    assert cup["current"]["phase"] == 1 and cup["current"]["round"] == 0, cup["current"]
    ko = call("/cup/%s/match" % cupid)["resources"][0]["body"]
    assert ko["gamesettings"].get("drawMode") == 2, "a knock-out tie must play extra time"
    assert next(t for t in ko["side"].values() if t["team"]["teamid"] == mine)

    # /cup/record: CupProxy takes only resources entries whose resource is /cup/record
    rec = call("/cup/record")
    assert rec["resources"][0]["resource"] == "/cup/record"
    assert rec["resources"][0]["body"]["_id"] == hc._state()["coaches"][0]["_id"]

    # /cup/<id>/leave: giving up pays nothing and drops the cup; /coach/me comes back with it
    left = call("/cup/%s/leave" % cupid)
    assert left["result"] and left["rewards"] == []  # still playing: no prize
    assert cupid not in hc._state()["cups"]
    assert any(r["resource"] == "/coach/me" for r in left["resources"])
    assert hc._state()["coaches"][0]["record"]["cup"]["abandonments"] == 1


# --------------------------------------------------------------------------- #
# 6. the module boundary itself
# --------------------------------------------------------------------------- #

def check_modules():
    """Every host name a module reaches for must exist, and no module may write to the proxy."""
    used, writes = {}, []
    for name in sorted(os.listdir(os.path.join(HERE, "modules"))):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(HERE, "modules", name), encoding="utf-8") as f:
            src = f.read()
        used[name] = sorted(set(re.findall(r"\bhc\.([A-Za-z_]\w*)", src)))
        writes += ["%s: %s" % (name, m) for m in
                   re.findall(r"^\s*hc\.([A-Za-z_]\w*)\s*=[^=]", src, re.M)]
    assert writes == [], "a write to the lazy proxy is silently lost: %s" % writes
    missing = [(mod, n) for mod, names in used.items() for n in names if not hasattr(hc, n)]
    assert missing == [], "modules reach for names http_config does not define: %s" % missing
    assert used["localization.py"] == [], "localization is the leaf: it must not need the host"
    # and the four modules are importable and answer their own entry points
    assert callable(cup_api.ep_cup_list) and callable(cup_api.ep_cup_new)
    assert callable(team_picker.ep_sortnation) and callable(team_picker.ep_authentic_team)
    assert localization.tr("no.such.key") is None and localization.tr("no.such.key", "d") == "d"


def check_import_order():
    """Either import order works: the modules resolve http_config on first use, not on import."""
    for cmd in ("import modules.team_picker, modules.cup_api, modules.tournaments, modules.localization",
                "import http_config; import modules.cup_api, modules.tournaments",
                "import modules.tournaments as t; t.tournament_config()"):
        r = subprocess.run([sys.executable, "-c", cmd], cwd=HERE,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert r.returncode == 0, "%s\n%s" % (cmd, r.stdout.decode("utf-8", "replace"))


# --------------------------------------------------------------------------- #

def run(name, fn, *args):
    started = time.time()
    fn(*args)
    print("  PASS  %-24s %5.1fs" % (name, time.time() - started))


def main():
    print("test_tournament: %s" % HERE)
    run("modules", check_modules)
    run("import order", check_import_order)
    run("config", check_config)
    run("shipped config", check_shipped_config)
    run("draw", check_draw)
    run("qualifiers", check_qualifiers)
    run("engine", check_engine)
    run("lineup", check_lineup)
    tmp = tempfile.mkdtemp()
    run("endpoints", check_endpoints, tmp)
    print("ALL PASS")


if __name__ == "__main__":
    main()
