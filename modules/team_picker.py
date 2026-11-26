"""Team picker: the nation -> league -> club wheels and the squad preview behind them.

The client's "Tim lain" club creation (StartupTeamCreationScreen / TeamSelectionUtil) and the cup
team picker (CupCreationScreen -> CupCreationTeamSelectionScene) are the same three steppers:
SORTNATION [nationid] -> SORTLEAGUE {nationid: [leagueid]} -> SORTTEAM {leagueid: [teamid]}. This
module answers them, and previews the squad under the cursor
(/static/info/authentic/team/<licensed|all>/<teamid>).

A team's league comes from gamedata/team_league.json (exported by tools/team_manager.py) and never
from a card's own leagueid: a player once on loan in Italy keeps leagueid 31 on a later Sevilla
card. National sides are league 78; the ones a cup may offer come from the tournament configs, the
only thing this module needs from modules/tournaments.py.
"""
import collections
import json
import os
from contextlib import closing

import gen_teams

from modules import hc  # http_config, resolved lazily (see modules/__init__.py)
from modules.tournaments import (tournament_config, tournament_entry_of_team, tournament_roster,
                                 tournament_teamids)

# tools/team_manager.py writes gamedata/team_league.json next to this package.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Club creation > "Tim lain" (StartupTeamCreationScreen + TeamSelectionUtil): nation -> league ->
# club steppers from SORTNATION [nationid], SORTLEAGUE {nationid: [leagueid]} and SORTTEAM
# {leagueid: [teamid]}; the picked club's squad is /static/info/authentic/team/licensed/<id>
# ({teamid, summary, team}, built by gen_teams.team like the six recommended clubs).
CLUB_MIN_PLAYERS = 16  # licensed 2017 cards a club needs to be listed
# Authoritative league -> teams from the client's chi.db.db2 (its `team` table), exported by
# tools/team_manager.py to gamedata/team_league.json as {leagueid: {chi_name, teamids}}. A card's
# own leagueid is unreliable: a player once on loan in Italy keeps leagueid 31 on a later Sevilla
# card (spid 88179645). The transfer market looks a club up by teamid, so do we: league membership
# comes from this table, never from the card.
TEAM_LEAGUE_FILE = os.path.join(ROOT, "gamedata", "team_league.json")
_TEAM_LEAGUE = None


def team_league():
    """The raw {leagueid: {chi_name, teamids}} map from gamedata/team_league.json."""
    global _TEAM_LEAGUE
    if _TEAM_LEAGUE is None:
        try:
            with open(TEAM_LEAGUE_FILE, encoding="utf-8") as f:
                _TEAM_LEAGUE = json.load(f)
        except (OSError, ValueError):
            _TEAM_LEAGUE = {}
    return _TEAM_LEAGUE

def _team_league_lookup():
    """{teamid: leagueid} reverse map built from team_league(), for club_index."""
    return {tid: int(lid) for lid, info in team_league().items()
            for tid in info.get("teamids", ())}


REST_OF_WORLD = 999998  # FO3's team for players of unlicensed clubs (26 LIVE has 91 such cards): not a club
# The picker's first wheel is a NATION name, so the national-team group (league 78) must not be
# labelled with one country. fifadb has no row for 300 and the client's NATIONNAMES_CHI names it
# "国家代表", which reads as the group header we want -- used for league 78 only.
NATION_PLACEHOLDER = 300
_CLUBS = []


def club_index():
    """[(nationid, [(leagueid, [teamid])])] of clubs with a full 2017 squad, strongest first (by
    their best eleven); a league's nation is its players' most common nationality. National
    teams are league 78 with their players' nationalteamid as the squad (gen_teams.team)."""
    if not _CLUBS:
        with closing(hc._db()) as c:  # current-season cards + 2017 cards of the players without one
            rows = c.execute(f"""SELECT p.spid, p.leagueid, p.teamid, p.nationalteamid, p.nationality, {hc.POSOVR}
                                 FROM players p JOIN permid m ON m.spid = p.spid AND m.licensed = 1
                                 WHERE p.spid / 1000000 = ?1 OR (p.spid / 1000000 = ?2 AND p.playerid NOT IN
                                       (SELECT playerid FROM players WHERE spid / 1000000 = ?1))""",
                             (gen_teams.CURRENT_SEASON, gen_teams.LEGACY_SEASON)).fetchall()
        # League membership is authoritative from the team table, not the card's own leagueid.
        # A club with no table row (e.g. a newly injected local team) falls back to its cards'
        # league; national teams are keyed by nationalteamid as before.
        tl = _team_league_lookup()
        squads, nats = {}, {}
        for spid, card_lid, tid, ntid, nat, ovr in rows:
            lid = tl.get(tid, card_lid)
            squads.setdefault((lid, tid), {})[spid] = ovr
            if ntid > 0:  # -1 / 0 = none
                squads.setdefault((gen_teams.NATIONAL_LEAGUE, ntid), {})[spid] = ovr
            nats.setdefault(lid, collections.Counter())[nat] += 1
        power = {k: sum(sorted(v.values())[-11:]) / 11 for k, v in squads.items()
                 if len(v) >= CLUB_MIN_PLAYERS and k[1] != REST_OF_WORLD}
        leagues = {}
        for (lid, tid), p in power.items():
            leagues.setdefault(lid, []).append((p, tid))
        by_nation = {}
        for lid, teams in leagues.items():
            teams.sort(reverse=True)
            # League 78 is not a country: label it with NATION_PLACEHOLDER ("国家代表") instead of
            # its players' most common nationality (which made the wheel read "印度").
            nat = (NATION_PLACEHOLDER if lid == gen_teams.NATIONAL_LEAGUE
                   else nats[lid].most_common(1)[0][0])
            by_nation.setdefault(nat, []).append((teams[0][0], lid, [t for _, t in teams]))
        # the national teams' nation last (they made it the strongest; the picker opens on the first).
        # One assignment: two first builds at once (a request and presence_loop) give the same list
        _CLUBS[:] = [(n, [(lid, ts) for _, lid, ts in sorted(ls, reverse=True)])
                     for n, ls in sorted(by_nation.items(), key=lambda kv: (
                         any(l[1] == gen_teams.NATIONAL_LEAGUE for l in kv[1]), -max(l[0] for l in kv[1])))]
    return _CLUBS


def ep_sortnation(query, body):
    """SORTNATION: the 3-wheel picker's first wheel. CupCreationScreen is the only client
    screen carrying the three SORT urls (checked: no other SWF in the archives contains
    SORTNATION/SORTLEAGUE/SORTTEAM), and the only cup that opens the picker is the World Cup
    tile, whose teams are all national teams (league 78). So the wheel offers that one entry --
    NATION_PLACEHOLDER (NATIONNAMES_CHI 300 "国家代表") -- and the player cannot scroll to a
    club group."""
    return hc._json([NATION_PLACEHOLDER])


def ep_sortleague(query, body):
    return hc._json({str(n): [lid for lid, _ in ls] for n, ls in club_index()})


def national_teamids():
    """league-78 ids the cup picker offers: every national side club_index can field, plus every
    configured pot team appended in place (club_index files a nation by the cards that kept
    nationalteamid, and the 14W set lost it on 547 of 1040, which drops Brazil). Appended rather
    than merged so the wheel's default entry does not move. A national side being here is also what
    tells ep_authentic_team that a teamid is a nation, not a club -- the picker offers China and
    India too (joinable.allteams stays true), and those are in no cup's pots."""
    base = next((ts for _, ls in club_index() for lid, ts in ls
                 if lid == gen_teams.NATIONAL_LEAGUE), [])
    have = set(base)
    return base + [t for t in tournament_teamids() if t not in have]


def ep_sortteam(query, body):
    """/static/db/SORTTEAM {leagueid: [teamid]}: the club picker above; Liga's creation screen
    counts my own league's clubs (the ones its calendar will use)."""
    st = hc._state()
    out = {str(lid): ts for _, ls in club_index() for lid, ts in ls}
    nations = national_teamids()
    if nations:
        out[str(gen_teams.NATIONAL_LEAGUE)] = nations
    if st["coaches"]:
        teamid = hc.my_teamid(st)
        lid = hc.roster_summary(teamid).get("league", {}).get("id", 0)
        out[str(lid)] = hc.league_teams(lid, teamid)
    return hc._json(out)


def ep_authentic_team(kind, teamid, query, body):
    """/static/info/authentic/team/licensed|all/<teamid>: a team's starter squad {teamid, summary,
    team} (responses/ files override for the recommended clubs). StartupPlayerSelectionScreen
    (after "Tim lain") looks up team.captain among team.players by id and dresses its 3D player
    in kit.availables.home/goalie.

    A national side takes the other branch: with cup.teamselect == "authentic" the cup's team
    picker previews the squad of the team under the cursor
    (CupCreationTeamSelectionScene.updateSelectedTeamInfo -> setPlayerStepper), and asks here for
    it -- the plain roster files one by teamid, which for a national team is empty. Every card
    comes back state "first": setPlayerStepper keeps only Enums.isFirstTeam(state) and
    getPlayerState(undefined) is SECOND team, i.e. an empty player wheel.

    Not every national side the picker offers is in a cup's pots (China, India: the wheel stays
    open via joinable.allteams), so tournament_entry_of_team has nothing for those; they borrow the
    card sources of the tournament whose picker this is (the authentic one -- the request carries
    no cup id)."""
    t = int(teamid)
    entry = tournament_entry_of_team(t)
    if entry is None and t in national_teamids():
        entry = next((e for e in tournament_config() if e.get("teamselect") == "authentic"), {})
    if entry is not None:
        r = tournament_roster(t, entry) or hc.roster(t)
        team = dict(r["team"], players=[dict(p, id=str(p["spid"]), state="first")
                                        for p in r["team"]["players"]])
    else:
        r = hc.roster(t)
        team = dict(r["team"], players=[dict(p, id=str(p["spid"])) for p in r["team"]["players"]])
    team["captain"] = str(team["squad"].get("captain") or "")
    return hc._json({**r, "team": team, "teamid": t, "kit": hc.team_kit(t)})
