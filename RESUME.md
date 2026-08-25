# Orangutan Dashboard — Resume Checklist

Live site: https://sergewich.github.io/orangutan-dashboard/
Repo: https://github.com/sergewich/orangutan-dashboard
Local path: C:\AI_work\Orangutan_dashboard (WSL: /mnt/c/AI_work/Orangutan_dashboard)

## 1. Open a WSL terminal and go to the project
```
wsl
cd /mnt/c/AI_work/Orangutan_dashboard
```

## 2. Sync with GitHub before doing anything else
The automated workflows (daily/monthly/weekly) commit directly to GitHub —
your local checkout falls behind on its own even if you haven't touched it.
```
git status
git pull
```

## 3. Launch Claude Code
```
claude
```
If it says "Login expired" — that's your Anthropic account login, separate
from GitHub. Fix with `/login` and follow the browser prompt.

## 4. Get oriented — ask Claude Code this first
```
Read CLAUDE.md and give me a current-state summary: what's in
.github/workflows/, whether .env has both GFW_API_KEY and FIRMS_API_KEY
set, whether Ollama is healthy, and whether local git is in sync with
origin/master. Don't change anything yet.
```

## 5. What's already automated
Daily: fire alerts. Monthly: deforestation, literature, threats.
Weekly: raw social/news scraping only. On push to master: site redeploys.

## 6. The one manual step, on purpose
`data/social/incidents.json` is never written by automation. Review drafts
via:
```
python3 scripts/review_server.py
```
then open http://localhost:8000/site/tabs/review.html

`review_server.py` already serves the whole site (not just the review page),
so don't also run step 7 alongside it — both bind port 8000 and you only
need one running at a time.

## 7. Preview the site locally (when NOT reviewing drafts)
```
python3 -m http.server 8000
```
(from repo root), then open http://localhost:8000/site/index.html
