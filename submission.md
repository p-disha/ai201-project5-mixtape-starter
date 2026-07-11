# Project 5 — Mixtape Bug Hunt — Submission

**Author:** p-disha
**Branch:** `bugfix/mixtape`

---

## AI Usage

I worked through this project with **Claude Code (Opus 4.8)** acting as a pair-programming
assistant, and I used it heavily during the two phases the brief calls out — codebase
navigation and debugging — not just for writing patch code. Being specific about what that
looked like:

**Codebase navigation.** After cloning the starter I had the assistant read every service,
route, model, and test file and summarize each module's responsibility, then trace two call
chains end-to-end (rate-a-song → `notification_service.rate_song`, and view-playlist →
`playlist_service.get_playlist_songs`). This is the source of the codebase map below. I
verified the map against the files myself — the models section in particular, since the
`playlist_entries` association table carries the `position` / `added_by` / `added_at` columns
that turn out to matter for Issues #3 and #5.

**Reproduction before diagnosis.** I insisted on reproducing every bug before changing a line.
I wrote a throwaway script (`repro_tmp.py`, not committed) that drove the service functions
against the seeded DB and against crafted state (e.g. a listening event stamped "yesterday
23:00" for Issue #2, a fresh rating for Issue #4). The assistant helped me build that harness
quickly. The empirical output is quoted in each RCA entry's "how you reproduced it" field.

**Where AI helped me understand something.** The most useful moment was Issue #3. Reading the
code, the `outerjoin` to `song_tags` *looks* like a textbook row-fan-out duplicate bug (one row
per tag). But when I actually ran `search_songs("Anthem")` it returned the song **once**, even
though the raw joined query returned **three** rows. I asked the assistant why, and it explained
that SQLAlchemy's legacy `Query` API automatically de-duplicates full-entity results by identity
map — so the fan-out is masked at the service layer in this version. I verified that myself by
running the raw joined-row query (3 rows) next to the service call (1 result). That nuance is
the difference between a surface-level RCA and a correct one, and it changed how I wrote the
entry.

**Where I had to verify or the AI was incomplete.** For Issue #2 the AI's first instinct was
that a 24-hour window is "basically today," which is wrong — the whole point of the bug is that a
rolling 24h window and a calendar-day boundary diverge in the morning. I confirmed the divergence
with the crafted-event repro (event 6.9h old, inside the 24h window, but before today's midnight)
rather than taking the explanation at face value. I also double-checked the ISO-weekday reasoning
for Issue #1 against Python directly (`datetime(2024,6,16).weekday()` → `6`) instead of trusting
recall.

Net: AI did the fast reading and call-chain tracing and helped me articulate root causes; every
diagnosis was confirmed by running the code with controlled inputs before I committed a fix.

---

## Codebase Map

Mixtape is a Flask + SQLAlchemy JSON API for a social music app. The organizing pattern is
strict and consistent: **routes parse/format, services hold all business logic, models define
data.** Every route immediately delegates to a service function and does nothing else but input
validation and `jsonify`.

### Main files and their roles

| File | Responsibility |
|------|----------------|
| `app.py` | Flask application factory (`create_app`). Instantiates the `SQLAlchemy` object `db`, configures the SQLite URI, registers the four blueprints under `/songs`, `/playlists`, `/users`, `/feed`, and calls `db.create_all()`. |
| `models.py` | All 8 SQLAlchemy models/tables. Entities: `User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`. Association tables: `friendships` (symmetric self-referential M2M on `User`), `song_tags` (M2M `Song`↔`Tag`), and `playlist_entries` (M2M `Playlist`↔`Song` **plus** `position`, `added_by`, `added_at` columns — playlist songs have an explicit order, not just insertion order). |
| `routes/songs.py` | `GET /songs/search?q=`, `GET /songs/<id>`, `POST /songs/<id>/rate`, `POST /songs/<id>/listen`. |
| `routes/playlists.py` | `POST /playlists/`, `GET /playlists/<id>`, `GET /playlists/<id>/songs`, `POST /playlists/<id>/songs`. |
| `routes/users.py` | `GET /users/<id>`, `GET /users/<id>/streak`, `GET /users/<id>/notifications`, `POST /users/notifications/<id>/read`. |
| `routes/feed.py` | `GET /feed/<id>/listening-now`, `GET /feed/<id>/activity`. |
| `services/streak_service.py` | `record_listening_event` (creates a `ListeningEvent` + updates streak), `update_listening_streak` (the day-delta streak rules), `get_streak`. |
| `services/feed_service.py` | `get_friends_listening_now` (recent-window feed, deduped to one row per friend), `get_activity_feed` (last N events, no recency filter). |
| `services/search_service.py` | `search_songs` (case-insensitive title/artist match), `get_song`. |
| `services/notification_service.py` | `create_notification`, `add_to_playlist` (adds song + notifies sharer), `rate_song` (saves/updates a `Rating`), `get_notifications`, `mark_as_read`. |
| `services/playlist_service.py` | `create_playlist`, `get_playlist_songs` (ordered by `position`), `get_playlist`, `get_user_playlists`. |
| `seed_data.py` | Drops + recreates all tables and seeds 5 users (with friendships), 13 songs (0/1/3-tag mixes), 3 playlists (5–7 songs), listening events (recent + 1–14 days old), streaks, and one example `song_added_to_playlist` notification. |
| `tests/` | `test_streaks.py`, `test_search.py`, `test_playlists.py` — pytest, each using an in-memory SQLite app fixture. Several tests already assert *post-fix* behavior and fail on the starter. |

### Data flow — "a friend adds my shared song to a playlist and I get notified"

This is the working notification path (the one Issue #4 says is missing for ratings), traced end to end:

1. `POST /playlists/<playlist_id>/songs` with `{song_id, added_by}` hits `add_song` in `routes/playlists.py`. It validates the two fields and calls `add_to_playlist(playlist_id, song_id, added_by)`.
2. `add_to_playlist` in `notification_service.py` loads the `Song`, the adding `User`, and the `Playlist` (raising `ValueError` for any missing — the route turns that into a 400).
3. If the song isn't already in `playlist.songs`, it appends it (writing a `playlist_entries` row) and commits.
4. **The notification step:** if `song.shared_by != added_by_user_id`, it calls `create_notification(user_id=song.shared_by, type="song_added_to_playlist", body="…added your song…")`. `create_notification` persists a `Notification` row addressed to the original sharer.
5. The sharer later calls `GET /users/<id>/notifications` → `get_notifications` → returns their `Notification` rows newest-first.

The key structural takeaway: **notifications are created by the service that handles the
interaction, guarded by an "actor ≠ owner" check.** `rate_song` handles the rating interaction
but never performs step 4 — that is Issue #4.

### Patterns I noticed

- **Route → service delegation is total.** No business logic lives in routes. To debug any endpoint you jump straight to the one service function it calls (README says exactly this).
- **Datetimes are UTC-aware** (`datetime.now(timezone.utc)`), but `last_listened_at` read back from SQLite can be naive — `update_listening_streak` defensively re-attaches `tzinfo`. Time-boundary bugs (#1, #2) live in this layer.
- **`.to_dict()` on every model** is the single serialization boundary; `Song.to_dict()` pulls tags via the `song_tags` relationship, so search does **not** need to join `song_tags` itself to return tags (relevant to #3).
- **Association tables with payload columns** (`playlist_entries.position`) mean list order is explicit and must be respected by queries (`ORDER BY position`) — relevant to #5.

---

## Root Cause Analysis

### Issue #1 — My listening streak keeps resetting (Sundays)

**How I reproduced it.** The starter test `test_streak_increments_on_sunday` (Saturday
2024-06-15 → Sunday 2024-06-16) failed with `assert 1 == 2`: a streak that should have gone to 2
reset to 1. I confirmed the trigger condition is specifically *the update landing on a Sunday*, matching kenji's report that it only ever happened on a Sunday.

**How I found the root cause.** Route path is `POST /songs/<id>/listen` → `record_listening_event`
→ `update_listening_streak`. Reading `update_listening_streak` in `services/streak_service.py`,
the increment branch was `elif days_since_last == 1 and today.weekday() != 6:`. The `!= 6` jumped
out. I verified against Python directly: `datetime.date.weekday()` returns `6` for Sunday
(Mon=0 … Sun=6). So on any Sunday, `today.weekday() != 6` is `False`.

**The root cause.** Python's `date.weekday()` returns `6` for Sunday. The increment branch
required `today.weekday() != 6`, so whenever the consecutive-day listen happened **on a Sunday**,
the condition was false and execution fell through to the `else` branch, which sets
`listening_streak = 1`. A perfectly valid Saturday→Sunday continuation was treated as a skipped
day and the entire streak was discarded. Every other weekday worked, which is exactly the
"only on Sundays" symptom.

**My fix and side-effect check.** I removed the spurious `and today.weekday() != 6` clause so the
branch is simply `elif days_since_last == 1:` — a listen exactly one calendar day after the last
one always continues the streak. Side-effects: I re-ran the full `test_streaks.py` suite (5/5
pass, up from 4/5). I specifically re-checked both sides of the day boundary that this branch
governs — same-day (`days_since_last == 0`, no change), consecutive-day (now +1 on every weekday
including Sunday), and skipped-day (`> 1`, still resets to 1) — so removing the clause fixed the
Sunday case without weakening the genuine reset-on-skip behavior.

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it.** Against the seeded "Friday Energy" playlist I compared the raw
`playlist_entries` row count to what the service returns:
`entries in DB: 7, returned by service: 6`. The missing one was always the highest-`position`
row — i.e. the most recently added, exactly as darius reported. The starter test
`test_playlist_returns_all_songs` (5 songs seeded) also failed, returning 4.

**How I found the root cause.** Path is `GET /playlists/<id>/songs` → `get_playlist_songs` in
`services/playlist_service.py`. The query is correct — it joins `playlist_entries` and orders by
`position` ascending. The defect is on the very last line: the return statement slices the
ordered list with `songs[:-1]`.

**The root cause.** `get_playlist_songs` builds the correctly-ordered list of songs, then returns
`[song.to_dict() for song in songs[:-1]]`. The `[:-1]` slice drops the last element of the list.
Because the list is ordered by ascending `position`, the last element is always the
highest-position row — the most recently added song. That is why adding a new song "freed" the
previously-missing one (it was no longer last) while hiding the newcomer (now last). The function
docstring even claims "This function returns all songs in the playlist," which the slice
contradicts.

**My fix and side-effect check.** I changed `songs[:-1]` to `songs` so every ordered row is
returned. Side-effects: `test_playlists.py` now passes 3/3 (was 1/3), including
`test_playlist_returns_songs_in_order` (order preserved — I only removed the truncation, not the
`ORDER BY position`) and `test_empty_playlist_returns_empty_list` (an empty list stays empty;
note the old `[:-1]` on an empty list also returned `[]`, so that edge case never changed).

### Issue #4 — Notified on playlist-add but not on rating

**How I reproduced it.** With a driver script: aaliya shares a song, kenji rates it 5 stars via
`rate_song`, then I read aaliya's notifications:
`aaliya notifications before rating: 0, after: 0` while `rating saved? True`. So the rating
persists but no notification is ever created — exactly aaliya's report. My committed regression
test `tests/test_notifications.py::test_rating_creates_notification_for_sharer` encodes this
(it asserts 1 notification; on the unfixed code it fails with `assert 0 == 1`).

**How I found the root cause.** The brief hint said the cause is architectural, so I compared the
two interaction handlers in `notification_service.py` line by line. `add_to_playlist` ends with a
guarded `create_notification(...)` call (`if song.shared_by != added_by_user_id:`). `rate_song`
ends at `db.session.commit(); return rating` — it saves the `Rating` and stops. There is no
`create_notification` call anywhere in `rate_song`. That asymmetry is the whole bug.

**The root cause.** Notifications in this app are created imperatively by the service that handles
each interaction; there is no model hook or event that fires automatically. `add_to_playlist`
does its part; `rate_song` was simply never given the equivalent step. So the rating is written to
the DB but the sharer is never told — no notification row exists for `GET /users/<id>/notifications`
to return. It is a missing step, not a typo or a wrong comparison.

**My fix and side-effect check.** I appended the notification step to `rate_song`, mirroring
`add_to_playlist` exactly: after the commit, `if song.shared_by != user_id:` create a
`song_rated` notification addressed to `song.shared_by`, with a body naming the rater, song, and
score. I placed it after the commit so a DB failure on the rating can't leave an orphan
notification, and I reused the existing `rater`/`song` objects already loaded above. Side-effect
checks: (a) rating your own song creates no notification — covered by
`test_rating_own_song_does_not_notify`; (b) the pre-existing playlist-add path is untouched;
(c) `rate_song` still returns the `Rating` and still supports the update-existing-rating path
(the notification fires on re-rating too, which is acceptable and matches the "actor interacted"
semantics). Full suite: see final review.

> **Out-of-scope observation (not fixed):** while writing a sanity test I found that
> `add_to_playlist` raises an `IntegrityError` when adding a *brand-new* song, because
> `playlist.songs.append(song)` populates only the FK columns and leaves the NOT-NULL
> `playlist_entries.position` / `added_by` columns unset. This is a genuine latent defect but it
> is **not** one of the five tracked issues and is unrelated to the rating-notification fix, so I
> deliberately left it alone to keep the fix targeted and did not include a test that depends on
> that path.

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it.** I crafted a deterministic case: I gave darius a single listening event
stamped "yesterday 23:00" and asked for nova's feed the next morning. Output:
`darius last listen=2026-07-10T23:00:00 (6.9h ago) … darius in feed (24h window)? True`, while
`would midnight-boundary include it? False`. So an event from last night still showed as
"listening now" this morning — precisely nova's complaint that darius's 11pm listen was still
visible at 9am. I re-verified the fix with a second script: a yesterday-23:00 listener is
excluded and a today-00:30 listener is included (`feed: ['kenji', 'simone']`, darius absent).

**How I found the root cause.** Path is `GET /feed/<id>/listening-now` →
`get_friends_listening_now` in `services/feed_service.py`. The recency filter was
`cutoff = datetime.now(timezone.utc) - RECENT_THRESHOLD` with `RECENT_THRESHOLD = 24 hours`, then
`ListeningEvent.listened_at >= cutoff`. The moment I saw "24 hours" I recognized the mismatch
with the requirement, which is *today*, not *the last 24 hours*.

**The root cause.** The feed used a **rolling 24-hour window** instead of a **calendar-day
boundary**. A rolling window and "today" only agree at midnight; they diverge for the rest of the
day. At 9am, `now - 24h` is 9am *yesterday*, so any event from yesterday 9am onward — including a
listen at 11pm last night — still satisfies `listened_at >= cutoff` and appears as "listening
now." The stale entries naturally aged out exactly 24 hours after they occurred, which is why
nova saw last night's listens "hang around until the same time the next day."

**My fix and side-effect check.** I replaced the rolling cutoff with the start of the current
calendar day (UTC midnight) via a small helper `_start_of_today(now)`
(`now.replace(hour=0, minute=0, second=0, microsecond=0)`), and removed the now-unused
`RECENT_THRESHOLD`/`timedelta`. Boundary checks on both sides: an event at 00:30 today is
included; an event at 23:00 yesterday is excluded (both demonstrated above). I confirmed I did not
touch `get_activity_feed`, which is intentionally *not* recency-filtered (its docstring says so) —
so the "recent activity" and "listening now" behaviors stay distinct. Full suite: 15/15 pass.

### Issue #3 — The same song shows up two or three times in search

**How I reproduced it.** Searching `"Anthem"` (Crown Heights Anthem has 3 tags in the seed data),
I compared the raw joined query to the service result:
`raw joined rows: 3 [('Crown Heights Anthem',), ('Crown Heights Anthem',), ('Crown Heights Anthem',)]`
versus `search_songs('Anthem') count: 1`. This is the most interesting entry: the **fan-out to 3
rows is real and reproducible**, but the *service* returned the song only once.

**How I found the root cause.** Path is `GET /songs/search?q=` → `search_songs` in
`services/search_service.py`. The query does
`db.session.query(Song).outerjoin(song_tags, Song.id == song_tags.c.song_id).filter(title/artist ilike).all()`.
The `outerjoin` to the `song_tags` association table is the culprit: it produces one output row
per matching (song, tag) pair, so a song with 3 tags yields 3 rows, a song with 1 tag yields 1,
and a song with 0 tags yields 1 (the outer join's NULL row) — which matches simone's report that
*some* songs repeat two or three times and others appear once, all from a single-song match.

**Why the service currently returns 1 anyway (the nuance I verified with AI's help).** I expected
`search_songs` to return 3 and it returned 1. I asked why, verified it myself, and confirmed:
SQLAlchemy's **legacy `Query` API automatically de-duplicates full-entity result rows by identity
map**, so the three identical `Song` rows collapse to one *at the service layer only*. That
implicit safety net is the reason the shipped `test_search_no_duplicates_multi_tag_song` passes on
the starter. It is not intended behavior and it is fragile: any query that selects columns instead
of the full entity, or the modern 2.0 `select()` API (which does not auto-unique), exposes the
duplicates. I demonstrated exactly this — a `query(Song.id)` column query returns **3 rows with
the join and 1 without** (see repro output above). So the join is a latent duplication bug that
happens to be masked by an ORM implementation detail.

**The root cause.** `search_songs` joins `song_tags` even though tags are neither filtered on nor
selected (title/artist drive the filter; `Song.to_dict()` loads tags via the relationship). The
join therefore only fans rows out per tag and serves no purpose — it is the mechanism behind the
reported duplicates.

**My fix and side-effect check.** I removed the unnecessary `.outerjoin(song_tags, …)` so the
query selects distinct `Song` entities directly, and dropped the now-unused `Tag`/`song_tags`
imports. This fixes the root cause regardless of which query API is used, rather than papering
over it with a defensive `.distinct()`. Side-effect checks: all 5 `test_search.py` tests pass
(single-tag, multi-tag, no-tag songs each appear exactly once; matching still works; no-match
still returns `[]`), and I confirmed each result still carries its `tags` list because
`Song.to_dict()` loads tags independently of the search query. Column-query demonstration above
confirms the fan-out is genuinely gone (3 → 1), not merely re-masked.
