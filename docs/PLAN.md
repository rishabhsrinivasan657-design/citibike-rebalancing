# Project Plan: Citi Bike Rebalancing

## The problem

Every morning, Citi Bike stations in Midtown run out of bikes, and stations
downtown fill up so people can't park. Lyft (who runs Citi Bike) pays crews to
drive vans around and move bikes from full stations to empty ones.

## What this project does

1. **Records** how many bikes are at every station, every minute.
2. **Predicts** which stations will be empty or full in the next 30–60 minutes.
3. **Plans** which bikes a van crew should move, for example:
   "Take 12 bikes from W 52nd St to Grand Central before 8:15."
4. **Checks** how much the plan would have helped, using past data. The score is
   how many minutes of "no bikes" or "no parking" the plan would have prevented,
   compared with doing nothing and with what Lyft's crews actually did.

## How each step works

Every step goes through the same four stages, in order:

1. **Plan:** decide what we're building and write down how we'll know it works.
2. **Design:** decide how it fits together (written in `DESIGN.md`).
3. **Build:** write the code, plus tests that check it.
4. **Test:** run the checks. The step is only "done" when every check passes.

A ✅ means the check has passed. A ⬜ means it hasn't been done yet.

---

## Step 0: Set up the project
**Status: done (Oct 19, 2026)**

Create the folder, the tools, and these planning documents.

- ✅ The automatic checks run and pass (81 Python tests + 8 dbt known-answer tests).

---

## Step 1: Record live station data
**Status: almost done. Recording 24/7 on GitHub since Oct 19, 2026; waiting on the 24-hour check.**

**What it does:** every minute, a program asks Citi Bike "how many bikes and empty
docks does each station have?" and saves the answer. Once an hour it also saves
the station list (names, locations, sizes).

**Why it's first:** Citi Bike doesn't publish this history, so we have to record it
ourselves. Every day we're not recording is data we can never get back.

**Checks**
- ✅ It reads the real Citi Bike data correctly (all 2,519 stations).
- ✅ It saves files in the right place, organised by date and hour.
- ✅ If the saving part crashes and restarts, nothing is lost.
- ✅ If the recording part restarts, it picks up where it left off.
- ⬜ It runs for 24 hours straight and catches at least 98% of the minutes.
- ✅ A version that runs free on GitHub was built and tested on the real data for
     3 minutes (every check succeeded, every row was saved, it stopped on time).
- ✅ If uploading fails, the files are kept and sent again later (tested).
- ✅ Switched on: a test run on GitHub uploaded to Hugging Face (Oct 19, 2026).

**Problems found while testing (all fixed)**
0. GitHub's scheduler skipped two runs on the first evening (Oct 19), leaving an
   80-minute gap (7:43–9:05pm New York time). Each run now starts the next one
   itself, 15 minutes before it ends; the schedule is only a backup.
1. The saving part sometimes started before it was ready to read data, got
   confused, and crashed. It now gets everything ready when it starts.
2. Most stations re-send the same numbers every minute even when nothing changed,
   so about 80% of what we saved was repeats. Now we only save a station when its
   numbers change, plus a check-in every 15 minutes. That's about 4× less data.

---

## Step 2: Download past data
**Status: done (Oct 19, 2026)**

**What it does:** downloads the information the predictions will learn from, for
**October 2025 to September 2026** (the 12 most recent months Citi Bike has published):
- **Past trips:** every Citi Bike ride: where and when it started and ended.
  About 45 million rides, 8.6 GB to download.
- **Weather:** hourly temperature, rain, snow and wind in Central Park. Two versions:
  - what the weather **actually was**, and
  - what the **forecast said** at the time. When predicting live, we'll only know
    the forecast, so the model should learn from forecasts too.
- **Holidays:** US and New York holidays.

**Where it runs:** on GitHub's free computers, not your laptop. (The first try ran
on the laptop and used so much memory that macOS showed an "out of memory" warning.
The data was deleted, and the heavy work moved to GitHub.)

**Where it goes:** a private Hugging Face dataset called `citibike-history`, as
compact Parquet files (about 4× smaller than the originals). It's re-runnable at
any time: months already saved are skipped unless Citi Bike changes the file.

**Things to watch out for (found while looking at the files)**
- Station IDs look like numbers (`7293.10`) but must be kept as text, or
  `7293.10` turns into `7293.1` and stops matching.
- File names inside each month's zip change from month to month.
- Times are New York local time, with no time zone written down.
- A ride that starts just before midnight at the end of a month is listed in *both*
  months' files (512 rides). That's Citi Bike's doing; Step 3 removes the extra copy.
- A few real station IDs have only one decimal (e.g. `6131.1` is E 24 St & Park
  Ave S), so "one decimal" doesn't mean an ID was damaged.

**Checks** (run on GitHub, Oct 19, 2026)
- ✅ Trip counts match Citi Bike's files exactly: 45,384,105 trips across all 12 months.
- ✅ No trip appears twice within the same month. (512 month-end rides appear in two
  months' files, as expected.)
- ✅ 98.4% of trips start at a station in the live feed (target 97%). Only 0.06% of
  trips have no start station at all.
- ✅ Station IDs are stored as text, so `7293.10` can't become `7293.1`.
- ✅ Weather has every hour, both versions: 9,192 hours with no gaps.
- ✅ Holidays include the obvious ones (46 holidays, 2025–2027).

## Step 3: Organise the data into clean tables
**Status: done (Oct 20, 2026)**

**What it does:** turns the raw files into clean, tidy tables that the later steps
read from, using **dbt** (a standard tool for building and testing data tables
with SQL). It runs on GitHub, not your laptop, and saves the finished tables to
the `citibike-history` dataset on Hugging Face.

**The tables**
- **Trips:** every ride once (month-end duplicates removed), with times in
  proper New York time, ride length, and the station each ride used.
- **Station history:** every version of every station, such as when one moved,
  changed size, or was renamed. Each version has "valid from" and "valid to"
  times. (This is called a "slowly changing dimension", or SCD2.)
- **Bikes at every station, every minute:** filling the gaps between readings,
  and marking minutes when the recorder was down or a station stopped reporting.
- **Lyft's van moves:** a sudden jump in a station's bikes that riders at that
  station would almost never cause at that time of day (less than a 1-in-1,000
  chance, even on a day twice as busy as usual), of at least 7 bikes. This lets us
  compare our plan with what Lyft really did.
- **Recording health:** one row per minute saying whether we have data.

**Automatic checks** run every time the tables are built: no duplicate rides,
no missing IDs, bikes never below zero or above a station's size, every trip's
station exists in the station history, and so on. A **freshness check** warns
if the newest live data is more than 30 minutes old and fails at 60. (Data is
uploaded every 15 minutes, so a tighter limit would raise false alarms.)

**Lineage:** dbt draws a diagram of which table is built from which.

**Checks** (first real build, Oct 20, 2026, on GitHub)
- ✅ All the automatic data checks pass: 41 passed, 2 warnings, 0 errors.
  - Warning: 10,239 rides (0.02%) last over a day. This is normal in Citi Bike data
    (unreturned bikes are closed out later), so it only warns.
  - Warning: 482 rides end before they start, **all 482 on the night the clocks went
    back** (Nov 2, 2025), as predicted. The files don't say which 1am a time means.
- ✅ The freshness check passes. On its very first run it **failed correctly**: it
  caught the real 80-minute recorder gap from the evening before.
- ✅ The minute table matches the raw readings at every moment they were taken.
- ✅ **Van moves checked by hand.** It took three versions of the rule:
  - **v1** (6+ bikes in 3 minutes, every station): 139 "visits" in ~3 hours.
    Several were probably rush-hour riders at busy stations.
  - **v2** (compare with each station's normal busyness): 5 of 10 clear vans,
    4 borderline 5–6-bike changes (possibly groups of friends renting together),
    and 1 wrong: Grove St PATH in Jersey City. Jersey City trips are in separate
    files we don't download, so its stations looked almost unused.
  - **v3** (at least 7 bikes; Jersey City and Hoboken skipped; a visit split by one
    rider in the middle counts as one): **10 of 10 look like real van visits**,
    8 clearly and 2 probably. 29 visits in ~4 hours of data.
  - Final confirmation comes when October's trips are published (mid-November):
    a real van visit shouldn't be explained by trips.

**Found while testing:** some of my own test edits silently didn't apply after the
code was reformatted, so two checks passed without testing anything. They're fixed,
and a new guard test makes sure those test cases stay in the data.

**What the tables hold so far:** 45.4M rides; 2,519 stations; 672,573
station-minutes (about 4 hours of live data; recorder up 69% of that window
because of the gap); 4.5% of station-minutes empty, 8.5% full.

**Known limit:** the trips for October 2026 won't be published until about
mid-November, so for now van moves are detected from the size of the jump
alone. When those trips arrive, we can confirm that no riders explain them.

## Step 4: Predict empty and full stations
**Status: not started**

**What it does:** for each station, predicts the chance it will be empty (or full)
in the next 30 and 60 minutes. It looks at things like how many bikes it has now,
how fast bikes are leaving, the time of day, the day of the week, and the weather.

**How we'll know it's good:** we compare it with two simple guesses:
- "It'll stay like it is now."
- "It'll do what it usually does at this time of day."

The model has to beat both. We only ever test it on days it hasn't seen, so the
results are honest.

**Checks**
- ⬜ Beats both simple guesses on a week of data it has never seen.
- ⬜ When it says "70% chance", it's right about 70% of the time.

---

## Step 5: Plan the van moves
**Status: done (Oct 19, 2026), with known limits below**

**What it does:** given how many bikes each station has now and how busy it's
expected to be, works out the best moves for a few vans. For each move: which
station to take bikes from, where to drop them, how many, and by when. For example:
"Van 1: take 12 bikes from W 52 St & 6 Ave to Grand Central, arrive by 8:12."

**How it decides (in two stages):**
1. **Choose the moves.** A maths model (integer programming) picks how many bikes
   to move between which stations, to avoid as many "no bikes" and "no parking"
   problems as possible. Stations about to run out sooner count more. Driving
   time counts against a move.
2. **Put them in order.** Each van gets a list of trips, in order, most urgent
   first. A trip that can't finish within the shift is dropped and reported.
   A separate checker then re-checks every limit, so an impossible move is never shown.

A delivery that arrives after a station has run empty still counts, but only for
the riders who come after the bikes arrive. (The first version wrongly treated
late deliveries as useless; the tests caught it.)

**Real limits it respects:**
- Each van carries at most 25 bikes.
- You can't take more bikes than a station has, or drop more than it has free docks.
- Driving takes time: about 12 km/h in Midtown traffic, plus time to park and
  load (Manhattan's street grid is used to measure distance, not straight lines).
- The vans' shift has a fixed length (60 minutes by default).

**Where the expected busyness comes from:** the past year of trips (Step 2),
averaged per station, per hour, for weekdays and weekends separately. This is
computed on GitHub, not your laptop. When the predictions from Step 4 exist,
they'll replace these averages.

**Where it starts:** one area, Midtown (about 1.5 km around Bryant Park), at 8am
on a weekday.

**Checks**
- ✅ It never breaks a limit: van over-filled, station over-filled or emptied
  below zero, or a van finishing after the shift. Tested on 12 hand-built cases
  and 15 random ones, and re-checked automatically on every plan.
- ✅ When moving bikes can't help, it suggests no moves.
- ✅ It plans a busy 8am Midtown weekday in **0.5 seconds** (target: under 30):
  121 stations, 3 vans.
- ✅ On that morning, the plan cuts riders who'd find no bike or no dock from
  **75 to 44 (41% fewer)**: no bike 36 → 16, no dock 39 → 28, moving 41 bikes in 6 trips.

**Known limits (to fix in later steps)**
- The bike counts were whatever the live feed showed when it ran (Monday evening),
  paired with typical 8am demand. Once the recorder has a real weekday morning,
  we'll use the 7:45am counts instead.
- Expected riders are 12-month averages, not predictions. Step 4 replaces them.
- The vans are under-used (41 bikes in an hour) and "no dock" improves less than
  "no bike". Full stations can only send bikes to the 10 roomiest stations, which
  is a simplification worth revisiting.

## Step 6: Test the plan on past days
**Status: not started**

**What it does:** replays real past days minute by minute, as if our vans had been
out there following the plan, and counts the "no bikes" and "no parking" minutes.

It also has to account for this: when a station is empty, nobody can take a bike,
so the data shows zero demand even though people wanted bikes. We estimate that
hidden demand from normal days.

**Checks**
- ⬜ With no vans, the replay matches what really happened.
- ⬜ A results report comparing three cases: no vans, Lyft's real moves, our plan.

---

## Step 7: Finish and present
**Status: not started**

- Send a Slack message when data stops arriving or a station is about to run out.
- Write a clear README with a diagram, the results, and the limits of the project.
- Record a short demo.
