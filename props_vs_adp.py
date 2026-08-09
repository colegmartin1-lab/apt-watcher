#!/usr/bin/env python3
"""props_vs_adp.py — turn player prop betting lines into a VOR draft board and
compare it against ADP.

Pipeline
--------
1. Read two-way (over/under) prop lines, convert American odds to implied
   probabilities, and de-vig each pair *proportionally*.
2. Recover an implied *mean* for each market rather than taking the posted line
   at face value:  mu = L + sigma * Phi^-1(p_over),  with sigma = CV[market] * L.
   A juiced line (e.g. -125/+105) pushes the de-vigged probability off 0.50, so
   the recovered mean sits above (or below) the number the book posted.
3. Convert every market to half-PPR points and sum to a player projection.
4. Apply the per-player availability haircut `gp_adj` from the ADP file.
5. Compute replacement level per position, with FLEX allocated greedily across
   RB/WR/TE, and derive VOR = proj_adj - replacement.
6. Rank by VOR, join ADP, and report edge = adp_rank - vor_rank.

Usage
-----
    python props_vs_adp.py --init                              # write templates
    python props_vs_adp.py --teams 12 --rb 2 --wr 2 --flex 1   # build the board
"""

import argparse
import os
import sys
from statistics import NormalDist

import pandas as pd

_N = NormalDist()  # standard normal; .cdf / .inv_cdf

# --- knobs -----------------------------------------------------------------

# Half-PPR scoring: points per unit of each market's mean.
SCORING = {
    "pass_yds": 0.04,   # 1 pt / 25 yds
    "pass_td": 4.0,
    "pass_int": -2.0,
    "rush_yds": 0.1,    # 1 pt / 10 yds
    "rush_td": 6.0,
    "rec": 0.5,         # half point per reception
    "rec_yds": 0.1,
    "rec_td": 6.0,
}

# Coefficient of variation per market. This is the knob that controls how far
# the juice moves the recovered mean off the posted line: sigma = CV * line.
# Larger CV => a given de-vig skew implies a larger shift in the mean.
CV = {
    "pass_yds": 0.22,
    "rush_yds": 0.30,
    "rec_yds": 0.35,
    "rec": 0.30,
    "pass_td": 0.40,
    "rush_td": 0.40,
    "rec_td": 0.40,
    "pass_int": 0.40,
}

# Positional yards-per-reception, used only to impute a reception mean when no
# reception prop exists for a player. Pass-catching backs vary the most here.
YPR = {"RB": 7.5, "WR": 12.5, "TE": 10.0, "QB": 0.0}

FLEX_POS = ("RB", "WR", "TE")

# --- team-total reconciliation knobs ---------------------------------------
# The sharper team market (win totals, season point totals) disciplines the
# softer individual-prop market. These constants back a team's implied volume
# out of its point total so the summed player props can be checked against it.
LEAGUE_AVG_PF = 380.0      # league-average season points scored (~22.4/gm)
PTS_PER_WIN = 26.0         # season points per win above/below the 8.5 baseline
POINTS_PER_OFF_TD = 6.95   # offensive TD + expected PAT
# Share of team points from offensive TDs (remainder: FGs, safeties, D/ST).
# NOTE: unlike the two identity checks, the point-total anchor is only as good
# as this constant. Calibrate it to your scoring environment before trusting
# TD_HOT; the ry_ratio and td_id_ratio checks are assumption-free and don't
# depend on it.
OFF_TD_SHARE = 0.72

# Reconciliation flag thresholds (ratios of prop-sum to identity/anchor).
RECO_TOL = 0.06            # |ratio - 1| within this => reconciles
RECO_HOT = 0.12            # ratio above 1+this => individual lines run hot

# ---------------------------------------------------------------------------


def implied_prob(american):
    """American odds -> implied probability (with vig)."""
    o = float(american)
    return (-o) / ((-o) + 100.0) if o < 0 else 100.0 / (o + 100.0)


def recover_mean(line, over_odds, under_odds, cv):
    """De-vig a two-way line proportionally and recover the implied mean.

    Returns (mu, p_over_devigged).
    """
    p_over_raw = implied_prob(over_odds)
    p_under_raw = implied_prob(under_odds)
    p_over = p_over_raw / (p_over_raw + p_under_raw)  # proportional de-vig
    # p_over = P(X > L) = Phi((mu - L)/sigma)  =>  mu = L + sigma*Phi^-1(p_over)
    sigma = cv * float(line)
    mu = float(line) + sigma * _N.inv_cdf(min(max(p_over, 1e-6), 1 - 1e-6))
    return mu, p_over


def load_props(path):
    """Read the props CSV and collapse it to one mean per (player, market)."""
    df = pd.read_csv(path)
    required = {"player", "pos", "team", "market", "line", "over", "under"}
    missing = required - set(df.columns)
    if missing:
        sys.exit(f"props file {path} missing columns: {sorted(missing)}")

    players = {}
    for _, r in df.iterrows():
        market = str(r["market"]).strip()
        if market not in CV:
            print(f"  ! skipping unknown market '{market}' for {r['player']}")
            continue
        mu, _p = recover_mean(r["line"], r["over"], r["under"], CV[market])
        p = players.setdefault(
            r["player"], {"pos": str(r["pos"]).strip().upper(),
                          "team": str(r["team"]).strip(), "means": {}}
        )
        p["means"][market] = mu
    return players


def impute_receptions(players):
    """Where no reception prop exists but receiving yards do, back catches out
    of the receiving-yards mean using a positional YPR constant."""
    for name, p in players.items():
        means = p["means"]
        p["rec_imputed"] = False
        if "rec" not in means and "rec_yds" in means:
            ypr = YPR.get(p["pos"], 0.0)
            if ypr > 0:
                means["rec"] = means["rec_yds"] / ypr
                p["rec_imputed"] = True


def project(players):
    """Sum each player's market means into a half-PPR projection."""
    rows = []
    for name, p in players.items():
        proj = sum(SCORING[m] * mu for m, mu in p["means"].items() if m in SCORING)
        rows.append(
            {"player": name, "pos": p["pos"], "team": p["team"],
             "proj": proj, "rec_imputed": p["rec_imputed"]}
        )
    return pd.DataFrame(rows)


def apply_adp(board, adp_path):
    """Join ADP + availability haircut; proj_adj = proj * gp_adj."""
    adp = pd.read_csv(adp_path)
    if "gp_adj" not in adp.columns:
        adp["gp_adj"] = 1.0
    adp["gp_adj"] = adp["gp_adj"].fillna(1.0)
    board = board.merge(adp[["player", "adp_rank", "gp_adj"]], on="player", how="left")
    board["gp_adj"] = board["gp_adj"].fillna(1.0)
    board["proj_adj"] = board["proj"] * board["gp_adj"]
    return board


def replacement_levels(board, starters, flex):
    """Replacement points per position with FLEX allocated greedily.

    Base starters are filled first; the remaining pool across RB/WR/TE competes
    for FLEX slots by proj_adj. The replacement level for a position is the
    proj_adj of the first player who does NOT start at that position.
    """
    groups = {
        pos: g.sort_values("proj_adj", ascending=False).reset_index(drop=True)
        for pos, g in board.groupby("pos")
    }

    # Greedy flex allocation from the leftovers beyond base starters.
    pool = []
    for pos in FLEX_POS:
        g = groups.get(pos)
        if g is None:
            continue
        base = starters.get(pos, 0)
        for _, row in g.iloc[base:].iterrows():
            pool.append((row["proj_adj"], pos))
    pool.sort(key=lambda t: t[0], reverse=True)

    flex_alloc = {pos: 0 for pos in FLEX_POS}
    for i in range(flex):
        if i < len(pool):
            flex_alloc[pool[i][1]] += 1

    repl = {}
    for pos, g in groups.items():
        rank = starters.get(pos, 0) + flex_alloc.get(pos, 0)  # first non-starter idx
        if len(g) == 0:
            repl[pos] = 0.0
        elif rank < len(g):
            repl[pos] = float(g.iloc[rank]["proj_adj"])
        else:
            repl[pos] = float(g.iloc[-1]["proj_adj"])  # shallow pool: worst rostered
    return repl, flex_alloc


def build_board(props_path, adp_path, teams, roster, flex):
    players = load_props(props_path)
    impute_receptions(players)
    board = project(players)
    board = apply_adp(board, adp_path)

    starters = {pos: teams * n for pos, n in roster.items()}
    repl, flex_alloc = replacement_levels(board, starters, teams * flex)
    board["repl"] = board["pos"].map(repl).fillna(0.0)
    board["vor"] = board["proj_adj"] - board["repl"]

    board = board.sort_values("vor", ascending=False).reset_index(drop=True)
    board["vor_rank"] = board.index + 1
    board["edge"] = board["adp_rank"] - board["vor_rank"]  # + => model ranks ahead of ADP
    return board, starters, flex_alloc


# --- team-total reconciliation ---------------------------------------------


def team_prop_aggregates(players):
    """Sum each team's propped means into the quantities the identities touch."""
    fields = ("pass_yds", "rec_yds", "pass_td", "rec_td", "rush_td")
    agg = {}
    for name, p in players.items():
        t = agg.setdefault(p["team"], {f: 0.0 for f in fields})
        for m, mu in p["means"].items():
            if m in fields:
                t[m] += mu
    return agg


def load_team_totals(path):
    """Read team win totals and season point totals.

    points_for is optional; when absent it is backed out of the win total via
    the documented linear map so a team line always has a scoring anchor.
    """
    df = pd.read_csv(path)
    if "team" not in df.columns:
        sys.exit(f"team totals file {path} needs a 'team' column")
    if "win_total" not in df.columns:
        df["win_total"] = float("nan")
    if "points_for" not in df.columns:
        df["points_for"] = float("nan")
    out = {}
    for _, r in df.iterrows():
        wt = r["win_total"]
        pf = r["points_for"]
        if pd.isna(pf) and not pd.isna(wt):
            pf = LEAGUE_AVG_PF + (float(wt) - 8.5) * PTS_PER_WIN  # implied from wins
        out[str(r["team"]).strip()] = {"win_total": wt, "points_for": pf}
    return out


def _ratio(num, den):
    return num / den if den else float("nan")


def reconcile(players, totals):
    """Check each team's summed props against the market identities and the
    point-total anchor. Returns (report_df, {team: flag})."""
    agg = team_prop_aggregates(players)
    rows = []
    for team, a in sorted(agg.items()):
        tot = totals.get(team, {})
        pf = tot.get("points_for", float("nan"))

        # Identity 1: team passing yards == team receiving yards.
        ry_ratio = _ratio(a["rec_yds"], a["pass_yds"])
        # Identity 2: team passing TDs == team receiving TDs.
        td_id_ratio = _ratio(a["rec_td"], a["pass_td"])
        # Anchor: offensive TDs implied by the point total vs summed prop TDs.
        # Passing TDs == receiving TDs, so fall back to rec_td when the QB
        # itself isn't propped, keeping the anchor usable on partial rosters.
        pass_td_est = a["pass_td"] if a["pass_td"] > 0 else a["rec_td"]
        prop_off_td = a["rush_td"] + pass_td_est  # rushing + passing scores
        impl_off_td = (float(pf) * OFF_TD_SHARE / POINTS_PER_OFF_TD
                       if not pd.isna(pf) else float("nan"))
        td_anchor = _ratio(prop_off_td, impl_off_td)

        # Every flag is an OVER-allocation flag. Under-allocation (receivers
        # summing to less than the QB, prop TDs below the point-total anchor) is
        # just depth you didn't prop, not a wrong line — so it never flags.
        flags = []
        if not pd.isna(ry_ratio) and ry_ratio > 1 + RECO_TOL:
            flags.append("REC>PASS")
        if not pd.isna(td_id_ratio) and td_id_ratio > 1 + RECO_HOT:
            flags.append("RECTD>PASSTD")
        if not pd.isna(td_anchor) and td_anchor > 1 + RECO_HOT:
            flags.append("TD_HOT")

        rows.append({
            "team": team,
            "win_tot": tot.get("win_total", float("nan")),
            "pf": pf,
            "pass_yds": a["pass_yds"], "rec_yds": a["rec_yds"], "ry_ratio": ry_ratio,
            "pass_td": a["pass_td"], "rec_td": a["rec_td"], "td_id_ratio": td_id_ratio,
            "prop_offtd": prop_off_td, "impl_offtd": impl_off_td, "td_anchor": td_anchor,
            "flags": ",".join(flags) if flags else "ok",
        })
    report = pd.DataFrame(rows)
    team_flag = dict(zip(report["team"], report["flags"]))
    return report, team_flag


def print_reconcile_report(report):
    show = report.copy()
    for c in ("pf", "pass_yds", "rec_yds", "pass_td", "rec_td",
              "prop_offtd", "impl_offtd"):
        show[c] = show[c].round(1)
    for c in ("ry_ratio", "td_id_ratio", "td_anchor"):
        show[c] = show[c].round(2)
    show["win_tot"] = show["win_tot"].round(1)
    cols = ["team", "win_tot", "pf", "pass_yds", "rec_yds", "ry_ratio",
            "pass_td", "rec_td", "td_id_ratio", "prop_offtd", "impl_offtd",
            "td_anchor", "flags"]
    print("\n=== Team-total reconciliation "
          "(sharper team market vs summed player props) ===")
    with pd.option_context("display.max_rows", None, "display.width", 200):
        print(show[cols].to_string(index=False))
    flagged = report[report["flags"] != "ok"]
    if len(flagged):
        print(f"\n{len(flagged)} team(s) do not reconcile — the individual lines "
              "on these teams are the suspect ones:")
        print("  " + ", ".join(f"{r.team} [{r.flags}]" for r in flagged.itertuples()))
    else:
        print("\nAll teams reconcile within tolerance.")
    print("  flags are over-allocation only; under-allocation is unpropped depth, not a bad line")
    print("  ry_ratio    = sum(rec_yds)/QB pass_yds   (>1.06 => receivers over-propped)")
    print("  td_id_ratio = sum(rec_td)/QB pass_td     (>1.12 => receiver TD lines run hot)")
    print("  td_anchor   = prop off TDs / TDs implied by point total (>1.12 => team runs hot)")


# --- templates -------------------------------------------------------------

PROPS_TEMPLATE = """player,pos,team,market,line,over,under
Christian McCaffrey,RB,SF,rush_yds,1050.5,-115,-105
Christian McCaffrey,RB,SF,rush_td,10.5,-125,105
Christian McCaffrey,RB,SF,rec,68.5,-110,-110
Christian McCaffrey,RB,SF,rec_yds,560.5,-110,-110
Christian McCaffrey,RB,SF,rec_td,3.5,110,-130
Bijan Robinson,RB,ATL,rush_yds,1125.5,-110,-110
Bijan Robinson,RB,ATL,rush_td,9.5,105,-125
Bijan Robinson,RB,ATL,rec_yds,430.5,-115,-105
Tyreek Hill,WR,MIA,rec,96.5,-115,-105
Tyreek Hill,WR,MIA,rec_yds,1275.5,-120,100
Tyreek Hill,WR,MIA,rec_td,8.5,-105,-115
CeeDee Lamb,WR,DAL,rec,98.5,-110,-110
CeeDee Lamb,WR,DAL,rec_yds,1350.5,-125,105
CeeDee Lamb,WR,DAL,rec_td,8.5,-110,-110
Amon-Ra St. Brown,WR,DET,rec_yds,1180.5,-110,-110
Amon-Ra St. Brown,WR,DET,rec_td,7.5,-110,-110
Sam LaPorta,TE,DET,rec,76.5,-110,-110
Sam LaPorta,TE,DET,rec_yds,820.5,-115,-105
Sam LaPorta,TE,DET,rec_td,6.5,-115,-105
Travis Kelce,TE,KC,rec,82.5,-110,-110
Travis Kelce,TE,KC,rec_yds,900.5,-110,-110
Travis Kelce,TE,KC,rec_td,5.5,-110,-110
Josh Allen,QB,BUF,pass_yds,3950.5,-110,-110
Josh Allen,QB,BUF,pass_td,29.5,-115,-105
Josh Allen,QB,BUF,pass_int,12.5,-110,-110
Josh Allen,QB,BUF,rush_yds,520.5,-115,-105
Josh Allen,QB,BUF,rush_td,6.5,-130,110
Patrick Mahomes,QB,KC,pass_yds,4300.5,-110,-110
Patrick Mahomes,QB,KC,pass_td,31.5,-120,100
Patrick Mahomes,QB,KC,pass_int,10.5,-110,-110
"""

ADP_TEMPLATE = """player,adp_rank,gp_adj
Christian McCaffrey,1,0.82
CeeDee Lamb,2,1.0
Tyreek Hill,3,0.95
Bijan Robinson,4,1.0
Amon-Ra St. Brown,6,1.0
Sam LaPorta,11,1.0
Travis Kelce,18,0.90
Josh Allen,22,1.0
Patrick Mahomes,25,1.0
"""


TEAM_TOTALS_TEMPLATE = """team,win_total,points_for
SF,11.5,480
ATL,9.5,410
MIA,10.5,455
DAL,10.5,470
DET,11.5,500
KC,11.5,460
BUF,11.5,485
"""


def write_templates():
    with open("props.csv", "w") as f:
        f.write(PROPS_TEMPLATE)
    with open("adp.csv", "w") as f:
        f.write(ADP_TEMPLATE)
    with open("team_totals.csv", "w") as f:
        f.write(TEAM_TOTALS_TEMPLATE)
    print("wrote props.csv, adp.csv, and team_totals.csv")
    print("  edit them, then run:  python props_vs_adp.py --teams 12 --rb 2 --wr 2 --flex 1")


# --- cli -------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description="Props-vs-ADP VOR draft board")
    ap.add_argument("--init", action="store_true", help="write template CSVs and exit")
    ap.add_argument("--props", default="props.csv")
    ap.add_argument("--adp", default="adp.csv")
    ap.add_argument("--teams", type=int, default=12)
    ap.add_argument("--qb", type=int, default=1)
    ap.add_argument("--rb", type=int, default=2)
    ap.add_argument("--wr", type=int, default=2)
    ap.add_argument("--te", type=int, default=1)
    ap.add_argument("--flex", type=int, default=1)
    ap.add_argument("--team-totals", default="team_totals.csv",
                    help="team win/point totals for reconciliation (if present)")
    ap.add_argument("--no-reconcile", action="store_true",
                    help="skip the team-total reconciliation pass")
    ap.add_argument("--out", default=None, help="optional CSV path for the full board")
    args = ap.parse_args(argv)

    if args.init:
        write_templates()
        return

    roster = {"QB": args.qb, "RB": args.rb, "WR": args.wr, "TE": args.te}
    board, starters, flex_alloc = build_board(
        args.props, args.adp, args.teams, roster, args.flex
    )

    # Team-total reconciliation: flag board rows whose team's props don't
    # reconcile with the sharper team market, so you know which VOR values
    # rest on the soft individual lines.
    team_flag = {}
    report = None
    if not args.no_reconcile and os.path.exists(args.team_totals):
        totals = load_team_totals(args.team_totals)
        report, team_flag = reconcile(load_props(args.props), totals)
    board["team_reco"] = board["team"].map(team_flag).fillna("-")

    print(f"\nLeague: {args.teams} teams | starters/team "
          f"QB{args.qb} RB{args.rb} WR{args.wr} TE{args.te} FLEX{args.flex}")
    print(f"Flex allocated greedily across RB/WR/TE: {flex_alloc}\n")

    show = board.copy()
    for c in ("proj", "proj_adj", "repl", "vor"):
        show[c] = show[c].round(1)
    show["gp_adj"] = show["gp_adj"].round(2)
    show["adp_rank"] = show["adp_rank"].astype("Int64")
    cols = ["vor_rank", "adp_rank", "edge", "player", "pos", "team",
            "proj", "gp_adj", "proj_adj", "repl", "vor", "rec_imputed", "team_reco"]
    with pd.option_context("display.max_rows", None, "display.width", 180):
        print(show[cols].to_string(index=False))

    print("\nedge = adp_rank - vor_rank  (positive => the model ranks him ahead "
          "of where ADP does; a value target)")
    print("team_reco = team-total reconciliation flag; anything but 'ok' means "
          "this row rests on lines the team market disputes")

    if report is not None:
        print_reconcile_report(report)
    if args.out:
        board.to_csv(args.out, index=False)
        print(f"\nfull board written to {args.out}")


if __name__ == "__main__":
    main()
