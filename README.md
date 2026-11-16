# CBB model consensus

Pulls game projections from Bart Torvik, Haslametrics, Massey, Talisman Red and Dunkel.
It lines them up game by game, averages them into a consensus spread and total,
and (optionally) compares that to the betting market.

| Site | Source page | Covers |
|---|---|---|
| Torvik | schedule.php (T-Rank line) | any date |
| Haslametrics | homepage "upcoming games" box | next 7 days |
| Massey | /cb/ncaa-d1/games | any date |
| Talisman Red | predictions.shtml | next ~3 days |
| Dunkel | /picks/ncaa-basketball (clicks "Load more" to reach the date) | upcoming days — shown but **not** in the average (set `in_consensus` to True in SITES to include it) |
| Sasser | davidsasser.com/cbb | **today only**, posted around midday CT - re-run in the afternoon to include it; tracked, not in the plain average |
| TruRanking | /predictions | **off** until its predictions return Nov 23; then try `--sites torvik,haslametrics,massey,talismanred,truranking` and check it finds games |

Only D-I vs D-I games are included; games against non-D-I teams are skipped.

## Setup (one time)

```
pip install -r requirements.txt
python -m playwright install chromium
```

Market lines come free from ESPN's public scoreboard feed (DraftKings opening and
current lines, no signup). An Odds API key is only a backup if ESPN is down:
get a free key at the-odds-api.com, then
- Mac/Linux: `export ODDS_API_KEY=yourkey`
- Windows: `setx ODDS_API_KEY yourkey` (then open a new terminal)

## Run

```
python cbb_consensus.py                    # today
python cbb_consensus.py --date 2026-11-08
python cbb_consensus.py --sites torvik,massey
```

Results go to `output/` as a CSV and an HTML table.

## Reading the table

**Blended columns (first group)** - the main model:
- **Blended spread / total** = the market line adjusted by each model's calibrated weight.
- **Pick / cover %** = the side the blend favors and its estimated chance to cover
  ("no edge" when the blend equals the market).
- **Bet?** = `BET` (green) when the chance is 54.5%+ and the line hasn't moved 2.5+ points
  against the pick since opening (`skip: line moved` otherwise). Until 300 games are graded,
  strong picks show as `lean` instead of `BET`.

**Comparison columns** (kept for tracking): the plain model average and its edge/lean,
Torvik on its own, model disagreement (SD), opening lines, and every model's own numbers.
Talisman Red is left out of the spread average (still in the total average); Dunkel is left
out of both. Change that with `spread_avg` / `total_avg` / `in_consensus` in SITES.

**Calibration** runs every morning after grading and is fitted on **bet results**, not
accuracy: for every graded game it asks whether the bet covered (or went over), and how
that related to each model's disagreement with the morning line. It's a logistic fit with
weights >= 0 (trust a model or ignore it, never fade it), recent games weighted more, and
a cap on how much one huge disagreement can count (big gaps have been losing). Shrinkage
and the cap are chosen by 5-fold cross-validation. A model only gets weight if, on games
the fit didn't see, it beats a coin flip AND its strong picks would have won more than
52.4%. Otherwise the sheet says "no edge". Results go to `output/model_weights.json`, and
the summary shows what a 3-point gap from each weighted model means in cover chance.

**Re-running** a day simply replaces that day's sheet. If a file is open in Excel, the new
one is saved next to it with the time added, and the run still finishes.

## Results tracking

The summary also shows a **last 7 days vs season** trend for each model, and the same
betting records graded against the **opening line**: if a model beats the opener but not
the morning line, the edge is in betting earlier in the day.

Every normal run first grades the last 3 days' sheets against ESPN final scores and
DraftKings closing lines, appends them to `output/results_log.csv`, and prints a summary:

- win-loss record of the spread and total leans, split by edge size;
- **CLV** (closing line value): how many points the line moved your way between the
  morning run and tip-off; consistently positive CLV is the best sign the edges are real;
- record when the models agree (SD <= 3) and the spread edge is 3+;
- each model's average miss against the final margin and total, next to the market's.

Run `python cbb_consensus.py --grade` (or `--grade 2026-11-05`) to grade and see the summary
without building a new sheet. A day only gets graded if the script built that morning's sheet
(it saves a `raw_<date>.csv` next to the output). Run it before tip-off each day.

## When something breaks

- **A site shows 0 games:** run `python cbb_consensus.py --dump`. The raw
  pages land in `cache/`; send the file and the parser can be fixed.
- **A team doesn't match** (e.g. a new nickname): add a row to
  `team_aliases.csv` in the form `Canonical Name,Alias As Shown On Site`.
- `--from-cache` re-parses the saved pages without downloading again.

## Self-test

`tests/` holds real pages saved on Nov 5, 2026. To check the parsers after a
change: copy them into `cache/` and run
`python cbb_consensus.py --date 2026-11-05 --from-cache`. You should get 43 games.
