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

- ✅ The automatic checks run and pass (27 tests).

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

**Problems found while testing (both fixed)**
1. The saving part sometimes started before it was ready to read data, got
   confused, and crashed. It now gets everything ready when it starts.
2. Most stations re-send the same numbers every minute even when nothing changed,
   so about 80% of what we saved was repeats. Now we only save a station when its
   numbers change, plus a check-in every 15 minutes. That's about 4× less data.

---

## Step 2: Download past data
**Status: not started**

**What it does:** downloads the information the predictions will learn from:
- **Past trips:** every Citi Bike ride from the last 12 months (where and when it
  started and ended). Citi Bike publishes these for free.
- **Weather:** hourly temperature and rain. Fewer people ride in the rain.
- **Holidays:** riding patterns change completely on holidays.

**Checks**
- ⬜ At least 97% of trips can be matched to a real station.
- ⬜ The number of trips per month matches Citi Bike's files.
- ⬜ No missing weather hours.

---

## Step 3: Organise the data into clean tables
**Status: not started**

**What it does:** turns the raw saved files into clean, tidy tables that are easy
to use. The most important ones:
- **Bikes at every station, every minute** (filling in the gaps between readings).
- **Moves made by Lyft's crews.** If a station suddenly gains 15 bikes and no trips
  explain it, a van must have dropped them off. This lets us compare our plan
  with what Lyft really did.

It also adds automatic data checks, and an alert if the data stops arriving.

**Checks**
- ⬜ All the automatic data checks pass.
- ⬜ Alert if the newest data is more than 15 minutes old.
- ⬜ 10 detected Lyft van moves checked by hand to confirm they're real.

---

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
**Status: not started**

**What it does:** given the predictions, works out the best moves for a few vans:
which station to take bikes from, where to drop them, how many, and by when. It
respects real limits: each van holds about 20–30 bikes, stations have a fixed
number of docks, and driving takes time. We start with one area (Midtown).

**Checks**
- ⬜ It never breaks a limit (over-filling a van or a station, or arriving late).
- ⬜ It finds a plan for a busy 8am Midtown morning in under 30 seconds.

---

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
