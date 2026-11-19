#!/usr/bin/env python3
"""Generate self-hosted GitHub stats and streak cards for the profile README.

Queries the GitHub GraphQL API and writes light and dark SVG variants to
assets/, so the README depends on no third-party image service.

Usage:
    GH_TOKEN=... python .github/scripts/generate_stats.py [--login USER] [--out DIR]
    python .github/scripts/generate_stats.py --placeholder   # no network, "pending" cards

Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

API_URL = "https://api.github.com/graphql"

PROFILE_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    name
    login
    createdAt
    followers { totalCount }
    pullRequests { totalCount }
    issues { totalCount }
    repositories(ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC,
                 first: 100, after: $cursor) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        stargazerCount
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""

YEAR_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      restrictedContributionsCount
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""

THEMES = {
    "light": {
        "bg": "#ffffff",
        "border": "#d0d7de",
        "title": "#1f2328",
        "text": "#1f2328",
        "muted": "#59636e",
        "accent": "#0969da",
        "fire": "#bf3989",
        "track": "#eaeef2",
    },
    "dark": {
        "bg": "#0d1117",
        "border": "#30363d",
        "title": "#e6edf3",
        "text": "#e6edf3",
        "muted": "#9198a1",
        "accent": "#4493f8",
        "fire": "#f778ba",
        "track": "#21262d",
    },
}

FONT = "'Segoe UI', -apple-system, BlinkMacSystemFont, Helvetica, Arial, sans-serif"
CARD_W, CARD_H = 460, 200


@dataclass
class Stats:
    login: str
    name: str
    stars: int = 0
    repos: int = 0
    followers: int = 0
    pull_requests: int = 0
    issues: int = 0
    commits: int = 0
    contributions: int = 0
    since_year: int = 0
    current_streak: int = 0
    current_start: dt.date | None = None
    current_end: dt.date | None = None
    longest_streak: int = 0
    longest_start: dt.date | None = None
    longest_end: dt.date | None = None
    languages: list[tuple[str, str, int]] = field(default_factory=list)  # name, color, bytes


# --------------------------------------------------------------------------- API


def graphql(token: str, query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-stats-generator",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL error: {payload['errors']}")
    return payload["data"]


def fetch_stats(token: str, login: str, today: dt.date) -> Stats:
    cursor = None
    stars = 0
    lang_bytes: dict[str, int] = {}
    lang_color: dict[str, str] = {}
    user = None
    while True:
        data = graphql(token, PROFILE_QUERY, {"login": login, "cursor": cursor})
        user = data["user"]
        if user is None:
            raise RuntimeError(f"User {login!r} not found")
        repos = user["repositories"]
        for repo in repos["nodes"]:
            stars += repo["stargazerCount"]
            for edge in repo["languages"]["edges"]:
                name = edge["node"]["name"]
                lang_bytes[name] = lang_bytes.get(name, 0) + edge["size"]
                lang_color[name] = edge["node"]["color"] or "#8b949e"
        if not repos["pageInfo"]["hasNextPage"]:
            break
        cursor = repos["pageInfo"]["endCursor"]

    stats = Stats(
        login=user["login"],
        name=user["name"] or user["login"],
        stars=stars,
        repos=user["repositories"]["totalCount"],
        followers=user["followers"]["totalCount"],
        pull_requests=user["pullRequests"]["totalCount"],
        issues=user["issues"]["totalCount"],
    )
    top = sorted(lang_bytes.items(), key=lambda kv: kv[1], reverse=True)[:6]
    stats.languages = [(n, lang_color[n], b) for n, b in top]

    created = dt.datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00")).date()
    stats.since_year = created.year
    days: dict[dt.date, int] = {}
    for year in range(created.year, today.year + 1):
        start = dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc)
        end = min(
            dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc),
            dt.datetime.now(dt.timezone.utc),
        )
        data = graphql(
            token,
            YEAR_QUERY,
            {"login": login, "from": start.isoformat(), "to": end.isoformat()},
        )
        coll = data["user"]["contributionsCollection"]
        stats.commits += coll["totalCommitContributions"]
        stats.contributions += coll["contributionCalendar"]["totalContributions"]
        for week in coll["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                d = dt.date.fromisoformat(day["date"])
                if d <= today:
                    days[d] = day["contributionCount"]

    apply_streaks(stats, days, today)
    return stats


# ----------------------------------------------------------------------- streaks


def apply_streaks(stats: Stats, days: dict[dt.date, int], today: dt.date) -> None:
    """Fill current/longest streak fields from a {date: count} mapping.

    The current streak survives a day with no contributions *today* (the day
    is not over yet), but breaks on any earlier empty day.
    """
    if not days:
        return
    first = min(days)
    run, run_start = 0, None
    d = first
    while d <= today:
        if days.get(d, 0) > 0:
            if run == 0:
                run_start = d
            run += 1
            if run > stats.longest_streak:
                stats.longest_streak = run
                stats.longest_start, stats.longest_end = run_start, d
        else:
            run = 0
        d += dt.timedelta(days=1)

    end = today if days.get(today, 0) > 0 else today - dt.timedelta(days=1)
    count, d = 0, end
    while days.get(d, 0) > 0:
        count += 1
        d -= dt.timedelta(days=1)
    stats.current_streak = count
    if count:
        stats.current_start, stats.current_end = d + dt.timedelta(days=1), end


# ----------------------------------------------------------------------- render


def fmt_num(n: int | None) -> str:
    if n is None:
        return "–"
    if n >= 10_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n:,}"


def fmt_range(start: dt.date | None, end: dt.date | None) -> str:
    if not start or not end:
        return "No active streak"
    def md(d: dt.date) -> str:
        return f"{d.strftime('%b')} {d.day}"
    if start.year != end.year:
        return f"{md(start)} '{start.year % 100:02d} – {md(end)} '{end.year % 100:02d}"
    if start == end:
        return f"{md(end)}, {end.year}"
    return f"{md(start)} – {md(end)}, {end.year}"


def card(theme: dict, title: str, body: str, label: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{CARD_W}" height="{CARD_H}" viewBox="0 0 {CARD_W} {CARD_H}" role="img" aria-label="{escape(label)}">
  <title>{escape(label)}</title>
  <style>
    text {{ font-family: {FONT}; }}
    .title {{ font-size: 16px; font-weight: 600; fill: {theme['title']}; }}
    .label {{ font-size: 13px; fill: {theme['muted']}; }}
    .value {{ font-size: 13px; font-weight: 600; fill: {theme['text']}; }}
    .big {{ font-size: 28px; font-weight: 700; fill: {theme['text']}; }}
    .big-accent {{ font-size: 28px; font-weight: 700; fill: {theme['fire']}; }}
    .small {{ font-size: 11px; fill: {theme['muted']}; }}
  </style>
  <rect x="0.5" y="0.5" width="{CARD_W - 1}" height="{CARD_H - 1}" rx="8" fill="{theme['bg']}" stroke="{theme['border']}"/>
  <text x="24" y="36" class="title">{escape(title)}</text>
{body}</svg>
"""


def render_stats(stats: Stats | None, theme: dict, login: str) -> str:
    s = stats
    rows = [
        ("Total stars", s.stars if s else None),
        ("Public repos", s.repos if s else None),
        ("Commits", s.commits if s else None),
        ("Pull requests", s.pull_requests if s else None),
        ("Issues", s.issues if s else None),
        ("Followers", s.followers if s else None),
    ]
    parts = []
    for i, (label, value) in enumerate(rows):
        col, row = divmod(i, 3)
        x = 24 + col * 218
        y = 66 + row * 24
        parts.append(f'  <text x="{x}" y="{y}" class="label">{label}</text>')
        parts.append(f'  <text x="{x + 194}" y="{y}" class="value" text-anchor="end">{fmt_num(value)}</text>')

    # Top-languages bar.
    bar_x, bar_y, bar_w, bar_h = 24, 150, CARD_W - 48, 8
    parts.append(f'  <text x="{bar_x}" y="{bar_y - 8}" class="small">Top languages (public repos)</text>')
    parts.append(f'  <clipPath id="bar"><rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="4"/></clipPath>')
    parts.append(f'  <rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="4" fill="{theme["track"]}"/>')
    langs = s.languages if s else []
    total = sum(b for _, _, b in langs) or 1
    x = float(bar_x)
    legend = []
    for name, color, size in langs:
        w = bar_w * size / total
        parts.append(f'  <rect x="{x:.2f}" y="{bar_y}" width="{w:.2f}" height="{bar_h}" fill="{escape(color)}" clip-path="url(#bar)"/>')
        x += w
        legend.append((name, color, 100 * size / total))
    lx, ly = bar_x, bar_y + 28
    for name, color, pct in legend:
        text = f"{name} {pct:.0f}%"
        width = 20 + len(text) * 6.0
        if lx + width > CARD_W - 24:
            break
        parts.append(f'  <circle cx="{lx + 4}" cy="{ly - 4}" r="4" fill="{escape(color)}"/>')
        parts.append(f'  <text x="{lx + 12}" y="{ly}" class="small">{escape(text)}</text>')
        lx += width
    if not langs:
        note = "No language data yet" if s else "Generated daily by GitHub Actions – first run pending"
        parts.append(f'  <text x="{lx}" y="{ly}" class="small">{note}</text>')

    name = s.name.split()[0] if s else login
    label = f"GitHub stats for {login}"
    return card(theme, f"{name}'s GitHub stats", "\n".join(parts) + "\n", label)


def render_streak(stats: Stats | None, theme: dict, login: str) -> str:
    s = stats
    cols = [
        (
            fmt_num(s.contributions if s else None),
            "Contributions",
            f"Since {s.since_year}" if s else "",
            "big",
        ),
        (
            fmt_num(s.current_streak if s else None),
            "Current streak",
            fmt_range(s.current_start, s.current_end) if s else "",
            "big-accent",
        ),
        (
            fmt_num(s.longest_streak if s else None),
            "Longest streak",
            fmt_range(s.longest_start, s.longest_end) if s else "",
            "big",
        ),
    ]
    parts = []
    col_w = (CARD_W - 48) / 3
    for i, (value, label, sub, cls) in enumerate(cols):
        cx = 24 + col_w * i + col_w / 2
        parts.append(f'  <text x="{cx:.1f}" y="104" class="{cls}" text-anchor="middle">{escape(value)}</text>')
        parts.append(f'  <text x="{cx:.1f}" y="132" class="label" text-anchor="middle">{escape(label)}</text>')
        if sub:
            parts.append(f'  <text x="{cx:.1f}" y="152" class="small" text-anchor="middle">{escape(sub)}</text>')
        if i:
            x = 24 + col_w * i
            parts.append(f'  <line x1="{x:.1f}" y1="72" x2="{x:.1f}" y2="160" stroke="{theme["border"]}"/>')
    if not s:
        parts.append(f'  <text x="{CARD_W / 2}" y="182" class="small" text-anchor="middle">Generated daily by GitHub Actions – first run pending</text>')
    label = f"GitHub contribution streak for {login}"
    return card(theme, "Contribution streak", "\n".join(parts) + "\n", label)


def write_cards(stats: Stats | None, login: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for theme_name, theme in THEMES.items():
        (out / f"stats-{theme_name}.svg").write_text(render_stats(stats, theme, login), encoding="utf-8")
        (out / f"streak-{theme_name}.svg").write_text(render_streak(stats, theme, login), encoding="utf-8")


# ------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--login", default=os.environ.get("GITHUB_REPOSITORY_OWNER", "TschangoMP"))
    parser.add_argument("--out", default="assets", type=Path)
    parser.add_argument("--placeholder", action="store_true", help="write 'pending' cards without calling the API")
    args = parser.parse_args()

    if args.placeholder:
        write_cards(None, args.login, args.out)
        return 0

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GH_TOKEN (or GITHUB_TOKEN) must be set", file=sys.stderr)
        return 1

    today = dt.datetime.now(dt.timezone.utc).date()
    stats = fetch_stats(token, args.login, today)
    write_cards(stats, args.login, args.out)
    print(
        f"{stats.login}: {stats.contributions} contributions, {stats.commits} commits, "
        f"streak {stats.current_streak} (longest {stats.longest_streak}), {stats.stars} stars"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
