import os
import requests
from django.http import JsonResponse
from django.shortcuts import render
from django.conf import settings
import pandas as pd
from pathlib import Path
from .forecast import get_forecast_data
from .fpl_data import get_fpl_data

TEAM_ID = os.getenv("FPL_TEAM_ID", "1897520")

def _get_last_gameweek_points(player_id: int) -> int:
    try:
        url = f"https://fantasy.premierleague.com/api/element-summary/{player_id}/"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return 0
        data = resp.json()
        if "history" in data and data["history"]:
            return data["history"][-1].get("total_points", 0)
    except Exception:
        pass
    return 0

def _get_fpl_team(manager_id: str):
    try:
        base = "https://fantasy.premierleague.com/api/"
        bootstrap = requests.get(f"{base}bootstrap-static/", timeout=15).json()
        elements = bootstrap["elements"]
        player_map = {p["id"]: p for p in elements}
        teams = {t["id"]: t["name"] for t in bootstrap["teams"]}
        positions = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

        current_gw = next((gw["id"] for gw in bootstrap["events"] if gw["is_current"]), None)
        if not current_gw:  # gÃ¼venlik
            current_gw = max([gw["id"] for gw in bootstrap["events"] if gw["is_next"] or gw["finished"]], default=1)

        picks = requests.get(f"{base}entry/{manager_id}/event/{current_gw}/picks/", timeout=15).json()
        if "picks" not in picks:
            return []

        out = []
        for pick in picks["picks"]:
            player = player_map.get(pick["element"])
            if not player:
                continue
            photo_id = str(player["photo"]).split(".")[0]
            photo = f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{photo_id}.png"

            out.append({
                "ID": player["id"],
                "web_name": player["web_name"],
                "name": f"{player['first_name']} {player['second_name']}",
                "team": teams.get(player["team"], "Unknown"),
                "position": positions.get(player["element_type"], "N/A"),
                "now_cost": round(player["now_cost"] / 10.0, 1),
                "points": int(player["total_points"]),
                "last_gw_points": _get_last_gameweek_points(player["id"]),
                "is_captain": pick.get("is_captain", False),
                "is_vice_captain": pick.get("is_vice_captain", False),
                "photo": photo,
                
                "starting": pick["position"] <= 11,
            })
        out.sort(key=lambda x: (not x["starting"], -x["last_gw_points"]))
        return out
    except Exception:
        return []

# ---------- Views ----------
def index(request):
    return render(request, "fpldash/index.html")

def api_myteam(request):
    return JsonResponse(_get_fpl_team(TEAM_ID), safe=False)

def api_data(request):
    df = get_fpl_data()
    return JsonResponse(df.to_dict(orient="records"), safe=False)

def api_suggestions(request):
    # Basit sabit Ã¶neriler; sonra akÄ±llandÄ±rÄ±labilir
    suggestions = [
        {"name": "Cole Palmer", "team": "Chelsea", "position": "MID", "reason": "In-form & good fixtures"},
        {"name": "Anthony Gordon", "team": "Newcastle", "position": "MID", "reason": "Consistent returns"},
        {"name": "Evan Ferguson", "team": "Brighton", "position": "FWD", "reason": "Budget forward"},
    ]
    return JsonResponse(suggestions, safe=False)

def api_forecast(request):
    try:
        # Limit can be overridden via query param, default 50
        try:
            limit = int(request.GET.get("limit", 50))
        except ValueError:
            limit = 50
        data = get_forecast_data(limit=limit)
        return JsonResponse(data, safe=False)
    except Exception as ex:
        return JsonResponse({"error": str(ex)}, status=500)

def api_player_summary(request, player_id: int):
    try:
        url = f"https://fantasy.premierleague.com/api/element-summary/{int(player_id)}/"
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        raw = resp.json()
        history = [
            {"round": h.get("round"), "total_points": h.get("total_points")}
            for h in (raw.get("history") or [])
            if isinstance(h, dict)
        ]
        return JsonResponse({"history": history}, safe=False)
    except Exception as ex:
        return JsonResponse({"error": str(ex)}, status=502)

from datetime import datetime, timedelta, timezone
import re
import xml.etree.ElementTree as ET


def _fetch_tweets_via_api(username: str, days: int = 7):
    token = os.getenv("TWITTER_BEARER_TOKEN")
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        uresp = requests.get(
            f"https://api.twitter.com/2/users/by/username/{username}",
            headers=headers,
            timeout=15,
        )
        if uresp.status_code != 200:
            return None
        uid = uresp.json().get("data", {}).get("id")
        if not uid:
            return None
        start_time = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        tresp = requests.get(
            f"https://api.twitter.com/2/users/{uid}/tweets",
            params={
                "max_results": 100,
                "exclude": "replies,retweets",
                "tweet.fields": "created_at,text",
                "start_time": start_time,
            },
            headers=headers,
            timeout=20,
        )
        if tresp.status_code != 200:
            return None
        out = []
        for t in tresp.json().get("data", []) or []:
            out.append({
                "created_at": t.get("created_at"),
                "text": t.get("text"),
                "url": f"https://x.com/{username}/status/{t.get('id')}"
            })
        return out
    except Exception:
        return None


essy_profile_re = re.compile(r"<time[^>]+datetime=\"([^\"]+)\"[\s\S]*?</time>[\s\S]*?<p class=\"timeline-Tweet-text\"[^>]*>([\s\S]*?)</p>")

def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()


def _fetch_tweets_via_syndication(username: str, days: int = 7):
    # Unofficial syndication endpoint used by embeds; HTML parsing fallback
    try:
        resp = requests.get(
            f"https://cdn.syndication.twimg.com/widgets/timelines/profile?screen_name={username}",
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        j = resp.json()
        body = j.get("body") or j.get("body_html") or ""
        items = essy_profile_re.findall(body)
        cutoff = datetime.utcnow() - timedelta(days=days)
        out = []
        for dt_str, html in items:
            try:
                ts = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            except Exception:
                continue
            if ts.replace(tzinfo=None) < cutoff:
                continue
            out.append({
                "created_at": ts.isoformat(),
                "text": _strip_html(html),
                "url": None,
            })
        return out
    except Exception:
        return None


def _fetch_tweets_via_nitter(username: str, days: int = 7):
    """Fetch recent posts via public Nitter RSS mirrors (no auth)."""
    mirrors = [
        "https://nitter.net",
        "https://nitter.poast.org",
        "https://nitter.fdn.fr",
        "https://nitter.privacydev.net",
        "https://nitter.moomoo.me",
    ]
    cutoff = datetime.utcnow() - timedelta(days=days)
    for base in mirrors:
        try:
            url = f"{base}/{username}/rss"
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200 or not r.text:
                continue
            root = ET.fromstring(r.text)
            channel = root.find("channel")
            if channel is None:
                continue
            items = channel.findall("item")
            out = []
            for it in items:
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                pub = it.findtext("pubDate") or ""
                try:
                    ts = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z")
                except Exception:
                    ts = None
                if ts is None or ts < cutoff:
                    continue
                out.append({
                    "created_at": ts.isoformat(),
                    "text": title,
                    "url": link,
                })
            if out:
                return out
        except Exception:
            continue
    return None


def api_pricechanges(request):
    username = request.GET.get("user", "fplpricechanges")
    try:
        days = int(request.GET.get("days", "7"))
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 30))

    # Prefer official API when token is present
    data = _fetch_tweets_via_api(username, days)
    if not data:
        # try Nitter RSS mirrors (no auth)
        data = _fetch_tweets_via_nitter(username, days)
    if not data:
        # fallback to syndication parsing
        data = _fetch_tweets_via_syndication(username, days) or []

    return JsonResponse(data, safe=False)

def api_pricechanges_fpl(request):
    try:
        # Use official FPL bootstrap-static to derive price changes
        base = "https://fantasy.premierleague.com/api/bootstrap-static/"
        resp = requests.get(base, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        elements = data.get("elements", [])
        teams = {t["id"]: t["name"] for t in data.get("teams", [])}
        positions = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

        out = []
        for e in elements:
            now_cost = e.get("now_cost", 0) / 10.0
            change_event = e.get("cost_change_event", 0) / 10.0 if e.get("cost_change_event") is not None else 0.0
            change_start = e.get("cost_change_start", 0) / 10.0 if e.get("cost_change_start") is not None else 0.0
            if change_event == 0:
                continue  # show only players who changed price within current event window
            out.append({
                "Player": e.get("web_name"),
                "Team": teams.get(e.get("team")),
                "Position": positions.get(e.get("element_type")),
                "Price": round(now_cost, 1),
                "Change_Event": change_event,
                "Change_Since_Start": change_start,
                "Status": "Riser" if change_event > 0 else "Faller",
            })

        # Sort risers first then fallers by magnitude
        out.sort(key=lambda x: (x["Status"] != "Riser", -abs(x["Change_Event"]), x["Player"]))
        return JsonResponse(out, safe=False)
    except Exception as ex:
        return JsonResponse({"error": str(ex)}, status=500)
