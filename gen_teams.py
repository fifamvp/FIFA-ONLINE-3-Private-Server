"""Build the starter-team responses from the client's own player DB (extract_fifadb.py).

StartupQuickCreationScreen reads /static/info/authentic/team/recommendation as an ARRAY of
    {teamid, summary:{numofselectableplayers}, team:{squad:{positions,captain,kickers}, players}}
and calls PlayerListUtil.createPlayerList(item.team, false). Players are sent as
{"spid", "expandme": "player"}: ResourceController.expandResource collects every object with
`expandme` and builds `staticdata` from the client's own DB (its `stats` is an Array with named
keys, which JSON cannot express). squad.positions = [{spid, pos}] with pos an index into
Enums.PLAYERPOSITION; players not in positions become reserves. The detail button reads
/static/info/authentic/team/all/<teamid> as {summary, team}. Rosters are the real teamid
links in fifadb (base cards preferred); a national team's are its players' nationalteamid.

Usage: python gen_teams.py [teamid ...]   (default: clubs whose crest is in the game cache)
"""
import json, os, sqlite3, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "gamedata", "fifadb.sqlite")
OUT = os.path.join(HERE, "responses")
CRESTS = r"D:\FO3ID\32795\_cache\games\fo3id\externalAssets\crests\large"
DEFAULT_TEAMS = [241, 243, 1, 9, 45, 22]  # Barcelona, Real Madrid, Arsenal, Liverpool, Juventus, Dortmund

# 4-4-2: (Enums.PLAYERPOSITION index, pitch x, y). x/y are FIFA-style formation offsets (0..1,
# y from own goal); SquadScreenHandler reads them from squad.positions[i].starting[2..3] and
# team.formations[].offsets.
FORMATION = [
    (0, 0.5, 0.02),                                                                    # GK
    (3, 0.85, 0.22), (4, 0.62, 0.16), (6, 0.38, 0.16), (7, 0.15, 0.22),                # RB RCB LCB LB
    (12, 0.85, 0.5), (13, 0.62, 0.46), (15, 0.38, 0.46), (16, 0.15, 0.5),              # RM RCM LCM LM
    (24, 0.62, 0.8), (26, 0.38, 0.8),                                                  # RS LS
]
LINES = {"DF": range(1, 9), "MF": range(9, 20), "FW": range(20, 28)}  # Enums.PLAYERPOSITION
# preferredposition1 values that are natural for each slot (keeps LB/RB, LM/RM on their side)
FITS = {0: {0}, 3: {3, 2}, 4: {4, 5, 1}, 6: {6, 5, 1}, 7: {7, 8}, 12: {12, 23}, 13: {13, 14, 10, 9, 11, 18},
        15: {15, 14, 10, 11, 9, 18}, 16: {16, 27}, 24: {24, 25, 21, 20, 22, 23}, 26: {26, 25, 21, 22, 20, 27}}
FIT_BONUS = 4  # a natural player keeps the slot unless an out-of-position one rates 4+ higher
FORMATION_ID = 1
# starting = [attacking participation, defensive participation, x, y]; 1 = medium
PARTICIPATION = 1
SELECTABLE = 18
# summary text the client shows as-is (TeamStaticInfo: name, nation.name, rival.name); team and
# nation names are not in fifadb. league/nation ids come from the roster.
TEAM_INFO = {241: ("FC Barcelona", "Real Madrid"), 243: ("Real Madrid", "FC Barcelona"),
             1: ("Arsenal", "Tottenham Hotspur"), 9: ("Liverpool", "Everton"),
             45: ("Juventus", "Inter"), 22: ("Borussia Dortmund", "FC Schalke 04")}
NATION_NAMES = {45: "Spanyol", 14: "Inggris", 27: "Italia", 21: "Jerman"}
# shirt numbers per formation slot (MenuScreen kicker data reads player.jersey; 0 = no number)
NUMBERS = {0: 1, 3: 2, 4: 4, 6: 5, 7: 3, 12: 7, 13: 8, 15: 6, 16: 11, 24: 9, 26: 10}


def jersey_name(c, playerid):
    r = c.execute("SELECT jersey FROM namesind WHERE playerid = ?", (playerid,)).fetchone()
    return r[0] if r else ""


LEGACY_SEASON = 88  # spid // 1e6 of the "2017" cards (fifaaddict season badge y2088)
FO4_SEASON = 300  # FIFA Online 4's 26 LIVE (tools/fo4_season.py)


# The game world (issue #38: "keep all original FIFA Online 3 teams, squads, players, and card
# seasons unchanged ... new players and card seasons added separately"): the admin page's
# Database tab picks it, the server reads it when it starts. "modern": clubs play their 26 LIVE
# cards once installed; "classic": FO3's own 2017 squads everywhere, the added seasons (25 / 26
# LIVE, WC18, own seasons) only to collect (packs, Market, admin page).
WORLD_FILE = os.path.join(HERE, "gamedata", "world.json")


def world():
    try:
        with open(WORLD_FILE, encoding="utf-8") as f:
            return "classic" if json.load(f).get("world") == "classic" else "modern"
    except (OSError, ValueError, AttributeError):
        return "modern"


def _current_season():
    """26 LIVE once tools/fo4_season.py has put it in fifadb (user: "nanti ganti default session
    ke 26"), else 2017; always 2017 in the classic world. Clubs take their 26 LIVE cards first and
    fill up with 2017 ones."""
    if world() == "classic" or not os.path.exists(DB):
        return LEGACY_SEASON
    c = sqlite3.connect(DB)
    try:
        lo = FO4_SEASON * 1000000
        hit = c.execute("SELECT 1 FROM players WHERE spid BETWEEN ? AND ? LIMIT 1", (lo, lo + 999999)).fetchone()
    except sqlite3.Error:
        hit = None
    finally:
        c.close()
    return FO4_SEASON if hit else LEGACY_SEASON


WORLD = world()  # as the server started (the admin page tells a newer choice apart)
CURRENT_SEASON = _current_season()
# fifadb leagueid of the national teams ("International"). A national team's squad is its
# players' nationalteamid: most of them are at clubs (teamid = the club), so building it from
# teamid alone left out Italy, England, Germany, Spain, ... (GitHub issue #24).
NATIONAL_LEAGUE = 78


def team_grade(avg):
    """summary.teamgrade = "Rating klub" stars, 0..5 in halves (TeamInfoNew.setTeamGrade), from
    the XI's average client rating (2017 cards): 76 = 5 stars (Real Madrid), -0.5 per 1.75."""
    return max(0.5, min(5.0, round((avg - 59) / 1.75) / 2))


def _hc():
    """http_config (the client's ratings, ROLE_WEIGHTS), also while it runs as the server's
    __main__ (a plain import there would load a second copy)."""
    m = sys.modules.get("__main__")
    return m if hasattr(m, "ROLE_WEIGHTS") else __import__("http_config")


def national_code(c, teamid):
    """A national team's nationality code: fifadb links the two 1:1 (England 1318 -> 14), but only
    on the cards that kept the link -- 547 of the 1040 14W cards carry nationalteamid -1 and
    Italy's 49 carry none at all, so the code is read from the cards that do have it."""
    r = c.execute("""SELECT nationality FROM players WHERE nationalteamid = ?
                     GROUP BY nationality ORDER BY count(*) DESC, nationality LIMIT 1""", (teamid,)).fetchone()
    return r[0] if r else None


def _team_filter(teamid, nation):
    """(SQL condition, params) for "this team's cards": by nationality for a national side, by
    teamid for a club. nationalteamid is only set on half the 14W cards (Italy: none), so a
    nationalteamid filter leaves a national squad half empty -- nationality is the real key."""
    if nation:
        return "p.nationality = ?", (int(nation),)
    return "? IN (p.teamid, p.nationalteamid)", (int(teamid),)


def current_rows(c, teamid, nation=None):
    """The normal squad pool: the team's current-season cards (as on fifaaddict) plus 2017 ones,
    older ones only for players who have no current card. With 26 LIVE current, a player who has
    a 26 LIVE card plays for that card's club only (his 2017 card is at his 2017 club).
    `nation` selects the pool by nationality instead (a national team)."""
    cond, args = _team_filter(teamid, nation)
    return c.execute(f"""SELECT p.* FROM players p JOIN permid m ON m.spid = p.spid AND m.licensed = 1
                         WHERE {cond}
                         AND (? = ? OR p.spid / 1000000 = ?
                              OR p.playerid NOT IN (SELECT playerid FROM players WHERE spid / 1000000 = ?))
                         ORDER BY (p.spid / 1000000 != ?), (p.spid / 1000000 != ?), (p.spid < 1000000), p.spid""",
                     args + (CURRENT_SEASON, LEGACY_SEASON, CURRENT_SEASON, CURRENT_SEASON,
                             CURRENT_SEASON, LEGACY_SEASON)).fetchall()


def _source_rows(c, teamid, src, nation=None):
    """The cards one card source offers, best first. {"team": T} means T's own cards whatever the
    squad is (a club's, e.g. China's "2016 中国最强"); anything else follows the squad's own key
    -- nationality for a national team, teamid for a club (see _team_filter)."""
    team = int(src["team"]) if src.get("team") else teamid
    nat = None if src.get("team") else nation
    if src.get("current"):
        return current_rows(c, team, nat)
    cond, args = _team_filter(team, nat)
    return c.execute(f"""SELECT p.* FROM players p
                         WHERE {cond} AND p.spid / 1000000 = ?
                         ORDER BY p.overallrating DESC""",
                     args + (int(src["season"]),)).fetchall()


def squad_pool(c, teamid, sources=None, nation=None):
    """Every card the sources offer, one list per source, in priority order."""
    return ([_source_rows(c, teamid, src, nation) for src in sources] if sources
            else [current_rows(c, teamid, nation)])


def squad_rows(c, teamid, sources=None, min_players=SELECTABLE, nation=None):
    """The cards a squad is built from, in priority order, one entry per card source:
    [{"season": 77}, {"season": 81, "team": 113395}, {"current": True}]

    - {"season": N} = the team's own cards of season N, no permid filter (only 172 of the 1040
      14W cards are licensed, so a licensed filter leaves ~13 per nation). For a national team
      "own" means its nationality, not nationalteamid (see _team_filter). {"season": N, "team": T}
      = another team's cards of that season -- China's best squad is "2016 中国最强" (team 113395,
      39 cards), which is filed under that club, not under nationalteamid 1413.
    - {"current": True} = the normal pool (current season + 2017, see current_rows).

    Sources are concatenated in order and de-duplicated by player (first source wins), so the
    earlier entries are the ones that make the squad; later ones only top it up to `min_players`.
    The first source is always taken whole (a nation with 23 14W cards fields all 23, not the best
    18). `sources=None` is the plain current-season pool the clubs use."""
    if not sources:
        return current_rows(c, teamid, nation)
    out, seen = [], set()
    for i, rows in enumerate(squad_pool(c, teamid, sources, nation)):
        whole = i == 0  # the first source decides the squad; the rest only fill the bench
        for r in rows:
            if r["playerid"] in seen:
                continue
            seen.add(r["playerid"])
            out.append(r)
            if not whole and len(out) >= min_players:
                return out
        if whole and len(out) >= min_players:
            return out
    return out


def player_line(pos):
    """Enums.PLAYERPOSITION index -> the line it belongs to."""
    return "GK" if pos == 0 else ("DF" if pos in LINES["DF"] else ("MF" if pos in LINES["MF"] else "FW"))


def fill_lines(c, teamid, sources, rows, mins, nation=None):
    """Append players until the squad meets the per-line minimums ({"GK": 1, "DF": 4, "MF": 5,
    "FW": 1}), taking the best ones still available. Picking 18 cards by rating does not give that
    for free: Austria's 14W set has no goalkeeper at all and Scotland's two defenders, so the XI
    would put an outfielder in goal. Cards the sources skip (unlicensed, older seasons) are the
    last resort -- an Austrian keeper exists in season 88 but not as a licensed card."""
    have = [player_line(r["preferredposition1"]) for r in rows]
    seen = {r["playerid"] for r in rows}
    pools = squad_pool(c, teamid, sources, nation) + [None]
    extra = []
    for ln, need in mins.items():
        short = int(need) - have.count(ln)
        for pool in pools:
            if short <= 0:
                break
            if pool is None:  # any card this team has, whatever its season or license
                cond, args = _team_filter(teamid, nation)
                pool = c.execute(f"""SELECT p.* FROM players p WHERE {cond}
                                     ORDER BY p.overallrating DESC""", args).fetchall()
            for r in pool:
                if short <= 0:
                    break
                if r["playerid"] in seen or player_line(r["preferredposition1"]) != ln:
                    continue
                seen.add(r["playerid"])
                extra.append(r)
                short -= 1
    if not extra:
        return rows
    return (rows[:max(0, 30 - len(extra))] + extra) if len(rows) + len(extra) > 30 else rows + extra


def team(c, teamid, sources=None, national=None, min_players=SELECTABLE, min_lines=None, by_nation=False):
    """A stored team {teamid, summary, team}. `sources` picks the card seasons (see squad_rows);
    `national` says the team is a national side even when its cards are filed under a club (China
    fields "2016 中国最强", whose cards carry nationalteamid -1), so the squad still counts as a
    national team for the league/nation fields; `min_lines` ({"GK": 1, "DF": 4, ...}) tops the
    squad up per line, see fill_lines; `by_nation` builds a national squad from its players'
    nationality instead of nationalteamid (which half the 14W cards lost)."""
    hc = _hc()
    if teamid <= 0:  # no card has one; nationalteamid -1 = none (would match 53k cards)
        return None
    nat = national_code(c, teamid) if by_nation else None
    if nat is not None:
        national = True  # a squad picked by nationality is a national side by construction
    rows = squad_rows(c, teamid, sources, min_players, nat)
    inferred = national is None
    national = any(r["nationalteamid"] == teamid for r in rows) if inferred else bool(national)
    if sources:
        # already in priority order: first source first, its cards by rating
        best = {}
        for r in rows:
            best.setdefault(r["playerid"], r)
        rows = list(best.values())[:30]
    else:
        best = {}
        # the club's current-season cards (as on fifaaddict); older seasons (former players, e.g.
        # a 2006 Ronaldinho at Barcelona) and season 0 (engine template cards, ratings 120+, not on
        # fifaaddict) only when the current squad is too small
        keep = (CURRENT_SEASON, LEGACY_SEASON)
        for r in rows:
            if r["spid"] // 1000000 in keep or len(best) < SELECTABLE:
                best.setdefault(r["playerid"], r)
        # current-season cards first (2017's overallrating column runs ~5 above the client's rating)
        rows = sorted(best.values(), key=lambda r: (r["spid"] // 1000000 != CURRENT_SEASON, -r["overallrating"]))[:30]
    if len(rows) < 11:
        return None
    if min_lines:  # 1 GK / 4 DF / 5 MF / 1 FW, before the XI is picked
        rows = fill_lines(c, teamid, sources, rows, min_lines, nat)
    # XI: best (player, slot) pairs by the client's rating at that slot (the "Attr" the squad
    # screens show); goalkeepers only in goal. A slot left over takes the best remaining player.
    at = lambda r, pos: sum(r[k] * w for k, w in hc.ROLE_WEIGHTS[hc.POS_ROLE[pos]].items()) // 100
    pick = {}
    for _, i, pos in sorted(((at(r, pos) + FIT_BONUS * (r["preferredposition1"] in FITS[pos]), i, pos)
                             for i, r in enumerate(rows) for pos, _, _ in FORMATION
                             if (pos == 0) == (r["preferredposition1"] == 0)), reverse=True):
        if pos not in pick and i not in pick.values():
            pick[pos] = i
    for pos, _, _ in FORMATION:
        if pos not in pick:
            pick[pos] = max((i for i in range(len(rows)) if i not in pick.values()), key=lambda i: at(rows[i], pos))
    positions = [{"spid": rows[pick[pos]]["spid"], "pos": pos, "starting": [PARTICIPATION, PARTICIPATION, x, y]}
                 for pos, x, y in FORMATION]
    numbers = {p["spid"]: NUMBERS[p["pos"]] for p in positions}
    subs = iter(range(12, 99))
    players = [{"spid": r["spid"], "expandme": "player", "price": r["overallrating"],
                "jersey": numbers.get(r["spid"]) or next(subs), "jerseyname": jersey_name(c, r["playerid"])}
               for r in rows]
    # kickers is an Array indexed by Enums.KICKER_TYPES (leftcorner, rightcorner, freekick,
    # penalty, freekick2); an Object here fails "Type Coercion ... to Array"
    starters = [p["spid"] for p in positions if p["pos"]]
    squad = {"positions": positions, "captain": rows[0]["spid"], "kickers": starters[-5:], "formation": FORMATION_ID}
    formations = [{"formationid": FORMATION_ID, "formationname": "4-4-2",
                   "positions": [f[0] for f in FORMATION], "offsets": [{"x": f[1], "y": f[2]} for f in FORMATION]}]
    league = NATIONAL_LEAGUE if national else max(set(r["leagueid"] for r in rows), key=[r["leagueid"] for r in rows].count)
    # a national side is named by its own id: a squad topped up from another team's cards (China's
    # 2016 set carries nationality 155) would otherwise show that team's country
    nation = teamid if (national and not inferred) else max(set(r["nationality"] for r in rows), key=[r["nationality"] for r in rows].count)
    name, rival = TEAM_INFO.get(teamid, ("", ""))
    # TeamInfoNew (club confirmation popup): TEAMNAME + crest by id, ovr.FW/MF/DF bars and
    # teamgrade stars: card ratings at each player's own position (the lobby HUD's
    # http_config.team_summary adds slot, grade, level and team color, as the squad list does)
    ovr = {p["spid"]: hc.card_ovr(p["spid"]) for p in positions}
    line = {k: round(sum(ovr[p["spid"]] for p in positions if p["pos"] in r) / sum(p["pos"] in r for p in positions))
            for k, r in LINES.items()}
    summary = {"teamid": teamid, "id": teamid, "numofselectableplayers": SELECTABLE, "name": name,
               "league": {"id": league}, "nation": {"id": nation, "name": NATION_NAMES.get(nation, "")},
               "rival": {"name": rival}, "ovr": line, "teamgrade": team_grade(sum(ovr.values()) / len(ovr))}
    return {"teamid": teamid, "summary": summary,
            "team": {"teamid": teamid, "squad": squad, "players": players, "formations": formations}}


def main():
    ids = [int(a) for a in sys.argv[1:]] or [t for t in DEFAULT_TEAMS
                                             if os.path.exists(os.path.join(CRESTS, f"s{t}.png"))]
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    items = [t for t in (team(c, i) for i in ids) if t]
    for t in items:
        with open(os.path.join(OUT, f"static_info_authentic_team_all_{t['teamid']}.json"), "w") as f:
            json.dump({"summary": t["summary"], "team": t["team"]}, f)
        print(t["teamid"], len(t["team"]["players"]), "players, captain", t["team"]["squad"]["captain"])
    with open(os.path.join(OUT, "static_info_authentic_team_recommendation.json"), "w") as f:
        json.dump(items, f)


if __name__ == "__main__":
    main()
