import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from django.http import JsonResponse
from django.shortcuts import render

from .cache import get_bootstrap, compute_team_fdr
from .fpl_data import get_fpl_data
from .forecast import get_forecast_data

TEAM_ID = os.getenv("FPL_TEAM_ID", "1897520")

# ---------- Helpers ----------

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
        data = get_bootstrap()
        elements = data["elements"]
        player_map = {p["id"]: p for p in elements}
        teams = {t["id"]: t["name"] for t in data["teams"]}
        positions = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

        events = data.get("events", [])
        current_gw = next((gw["id"] for gw in events if gw["is_current"]), None)
        if not current_gw:
            current_gw = max(
                [gw["id"] for gw in events if gw.get("is_next") or gw.get("finished")],
                default=1,
            )

        picks_resp = requests.get(
            f"https://fantasy.premierleague.com/api/entry/{manager_id}/event/{current_gw}/picks/",
            timeout=15,
        )
        picks = picks_resp.json()
        if "picks" not in picks:
            return []

        out_base = []
        for pick in picks["picks"]:
            player = player_map.get(pick["element"])
            if not player:
                continue
            photo_id = str(player["photo"]).split(".")[0]
            photo = f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{photo_id}.png"
            out_base.append({
                "ID": player["id"],
                "web_name": player["web_name"],
                "name": f"{player['first_name']} {player['second_name']}",
                "team": teams.get(player["team"], "Unknown"),
                "position": positions.get(player["element_type"], "N/A"),
                "now_cost": round(player["now_cost"] / 10.0, 1),
                "points": int(player["total_points"]),
                "last_gw_points": 0,  # filled in below
                "is_captain": pick.get("is_captain", False),
                "is_vice_captain": pick.get("is_vice_captain", False),
                "photo": photo,
                "starting": pick["position"] <= 11,
            })

        # Fetch last-GW points for all players in parallel
        with ThreadPoolExecutor(max_workers=15) as pool:
            futures = {
                pool.submit(_get_last_gameweek_points, entry["ID"]): i
                for i, entry in enumerate(out_base)
            }
            for future in as_completed(futures):
                idx = futures[future]
                out_base[idx]["last_gw_points"] = future.result()

        out_base.sort(key=lambda x: (not x["starting"], -x["last_gw_points"]))
        return out_base
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
    """
    Return top-3 value picks per position based on form, PPG, price, and
    fixture difficulty (FDR) for the next 3 gameweeks.

    Score = (form×0.6 + ppg×0.4) × (6 - fdr) / price
    A lower FDR (easier fixture) raises the score; a higher price lowers it.
    """
    try:
        data = get_bootstrap()
        elements = data["elements"]
        teams = {t["id"]: t["name"] for t in data["teams"]}
        positions = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

        team_fdr = compute_team_fdr(data, n_gws=3)

        scored = []
        for e in elements:
            price = e.get("now_cost", 0) / 10.0
            if price <= 0:
                continue
            form = float(e.get("form") or 0)
            ppg = float(e.get("points_per_game") or 0)
            team_id = e.get("team")
            fdr = team_fdr.get(team_id, 3.0)
            # Clamp FDR bonus so score stays positive
            fdr_bonus = max(6.0 - fdr, 0.5)
            score = (form * 0.6 + ppg * 0.4) * fdr_bonus / price

            scored.append({
                "name": e.get("web_name"),
                "team": teams.get(team_id, ""),
                "position": positions.get(e.get("element_type"), ""),
                "price": round(price, 1),
                "form": round(form, 1),
                "ppg": round(ppg, 1),
                "predicted_score": round((form + ppg) / 2, 2),
                "total_points": int(e.get("total_points") or 0),
                "fdr_next3": fdr,
                "score": round(score, 3),
            })

        result = []
        for pos in ("GK", "DEF", "MID", "FWD"):
            top3 = sorted(
                (p for p in scored if p["position"] == pos),
                key=lambda x: -x["score"],
            )[:3]
            for p in top3:
                p["reason"] = (
                    f"Form {p['form']} | PPG {p['ppg']} | "
                    f"FDR {p['fdr_next3']} | \u00a3{p['price']}m"
                )
                result.append(p)

        return JsonResponse(result, safe=False)
    except Exception as ex:
        return JsonResponse({"error": str(ex)}, status=500)


def api_forecast(request):
    try:
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


# ---------- Twitter / price-change helpers ----------

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
                "url": f"https://x.com/{username}/status/{t.get('id')}",
            })
        return out
    except Exception:
        return None


_essy_profile_re = re.compile(
    r"<time[^>]+datetime=\"([^\"]+)\"[\s\S]*?</time>"
    r"[\s\S]*?<p class=\"timeline-Tweet-text\"[^>]*>([\s\S]*?)</p>"
)


def _strip_html(s: str) -> str:
    return (
        re.sub(r"<[^>]+>", " ", s or "")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .strip()
    )


def _fetch_tweets_via_syndication(username: str, days: int = 7):
    try:
        resp = requests.get(
            f"https://cdn.syndication.twimg.com/widgets/timelines/profile?screen_name={username}",
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        j = resp.json()
        body = j.get("body") or j.get("body_html") or ""
        items = _essy_profile_re.findall(body)
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
            out = []
            for it in channel.findall("item"):
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
    data = _fetch_tweets_via_api(username, days)
    if not data:
        data = _fetch_tweets_via_nitter(username, days)
    if not data:
        data = _fetch_tweets_via_syndication(username, days) or []
    return JsonResponse(data, safe=False)


def api_pricechanges_fpl(request):
    try:
        data = get_bootstrap()
        elements = data.get("elements", [])
        teams = {t["id"]: t["name"] for t in data.get("teams", [])}
        positions = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

        out = []
        for e in elements:
            now_cost = e.get("now_cost", 0) / 10.0
            change_event = (
                e.get("cost_change_event", 0) / 10.0
                if e.get("cost_change_event") is not None
                else 0.0
            )
            change_start = (
                e.get("cost_change_start", 0) / 10.0
                if e.get("cost_change_start") is not None
                else 0.0
            )
            if change_event == 0:
                continue
            out.append({
                "Player": e.get("web_name"),
                "Team": teams.get(e.get("team")),
                "Position": positions.get(e.get("element_type")),
                "Price": round(now_cost, 1),
                "Change_Event": change_event,
                "Change_Since_Start": change_start,
                "Status": "Riser" if change_event > 0 else "Faller",
            })

        out.sort(key=lambda x: (x["Status"] != "Riser", -abs(x["Change_Event"]), x["Player"]))
        return JsonResponse(out, safe=False)
    except Exception as ex:
        return JsonResponse({"error": str(ex)}, status=500)
