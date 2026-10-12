# ORB trading journal

One row per session — including no-trade days (they count as discipline wins).
Score outcomes in R, never dollars: R is what makes days comparable.
Weekly review: win rate over 33% at 2R = profitable. Mistakes column trending
to zero matters MORE than the R column in month one.

| Date | Instrument | Range (pts) | Range H/L | Signal | Vol vs avg | My call (before bot) | Bot's call | Entry | Stop | Target | Outcome (R) | Mistakes | What I felt |
|------|-----------|-------------|-----------|--------|-----------|----------------------|------------|-------|------|--------|-------------|----------|-------------|
| 2026-07-10 | NAS100 |  |  |  |  |  |  |  |  |  |  |  |  |

## Mistake codes
- **W** — wanted to enter on a wick poke (body closed inside)
- **V** — wanted to enter on a low-volume close
- **D** — wrong direction (faded the break)
- **E** — early (inside the range / before 9:45)
- **L** — late (chased after missing the signal candle)
- **R** — revenge urge (wanted a 2nd trade after a loss)
- **S** — wanted to widen/move the stop or exit early

## Weekly tally
| Week of | Sessions | Trades | Wins | Losses | Skips | Win % | Net R | Mistakes |
|---------|----------|--------|------|--------|-------|-------|-------|----------|
|         |          |        |      |        |       |       |       |          |

## Rules of the journal
1. Write "my call" BEFORE looking at the bot's log — the comparison is the training.
2. A skipped chop day is a +0R WIN. Log it proudly.
3. A -1R loss with zero mistakes is a GOOD session. A +2R win with a mistake
   (e.g. entered on a wick that happened to work) is a BAD session — flag it.
4. Never journal dollars. Only R.
