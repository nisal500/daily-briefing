# Daily Briefing → ntfy

Every day at 7:30 AM Vancouver time: top news, Vancouver weather, 3 priorities.
Runs free on GitHub Actions. Your Mac can be off. No email, no calendar, no API keys.

## How it works
- GitHub runs `briefing.py` at about 5:10, 6:10 and 7:10 AM (backups in case one run is late or skipped).
- The first run builds the briefing and tells ntfy to deliver it at exactly **7:30 AM**.
- Later runs ask ntfy "is today's briefing already sent or scheduled?" and stop if yes. **One message per day.**
- If that check fails, the run does not send (no duplicate risk); the next backup run tries again.
- Sources: BBC + CBC RSS (stories < 30 h old), Open-Meteo (temps, rain), Environment Canada (warnings).
- Missing data is shown as "unavailable". Nothing is made up. Priorities are rule-based, never personal appointments.

## Setup (about 10 minutes, all in the browser)
1. **Phone:** install the **ntfy** app (App Store / Google Play). Tap **+**, enter topic
   `nisal-daily-briefing-8f3c9a72e1b64d05a7c2f91b6e4d830a`, keep server `ntfy.sh`, tap Subscribe.
   Allow notifications when asked.
2. **GitHub:** create a free account → **New repository** → name `daily-briefing` → **Private** → Create.
3. **Add files:** *Add file → Upload files* → upload `briefing.py` (and `tests/` if you like).
   Then *Add file → Create new file*, name it exactly `.github/workflows/daily-briefing.yml`,
   paste the workflow file contents, and commit.
4. **Secret:** *Settings → Secrets and variables → Actions → New repository secret*
   Name `NTFY_TOPIC`, value = your topic name above.
5. **Test:** *Actions* tab → *Daily Briefing* → **Run workflow** → mode `test` → Run.
   Your phone should get "TEST – Daily Briefing …" within a minute.
6. **Done.** The schedule turns on by itself. Tomorrow, check the run log for:
   `OK: ntfy accepted message (scheduled for 07:30 Vancouver)`.

## Security — please read
- The topic name works like a password. **Anyone who knows it can read and send messages to it.**
- An unguessable topic is *not* private, authenticated messaging. Messages are not end-to-end encrypted
  and ntfy.sh keeps them for about 12 hours.
- This briefing contains only public news and weather. Never add personal data to it.
- Keep the topic in the GitHub Secret only, never in code. If it leaks, pick a new topic and update
  the app and the secret.
- For real access control: ntfy Pro (paid) reserved topic or a self-hosted ntfy server, then add an
  access token as secret `NTFY_TOKEN` (the script already supports it).

## Maintenance
- **Cost:** free. Uses about 90 of the 2,000 free private-repo minutes each month.
- **Failures:** GitHub notifies you if a run fails. Open *Actions* → the run → read the log.
  Logs show which source failed; they never show your topic.
- **Pause:** *Actions → Daily Briefing → ⋯ → Disable workflow*. Enable again the same way.
- **Change news sources:** edit `FEEDS` in `briefing.py`.
- **Time zone:** B.C. uses permanent Pacific Time (UTC-7) since Nov 2026. The script checks this and
  never sends an hour late, even if GitHub's server has old time-zone data.

## Troubleshooting
| Problem | Fix |
|---|---|
| No test notification | Check phone topic spelling, app notification permission, and the `NTFY_TOPIC` secret. |
| Run log says `NTFY_TOPIC is not set` | Secret name must be exactly `NTFY_TOPIC`. |
| "duplicate check failed" | ntfy.sh was unreachable; the next backup run retries. |
| "skip: too late" | All runs were delayed past 11 AM (rare). Nothing sent that day. |
| A news source keeps failing | The feed URL may have changed; replace it in `FEEDS`. |
| Want a preview without sending | Run workflow with mode `dry-run` and read the log. |

Run tests locally (optional): `python3 tests/test_briefing.py`
