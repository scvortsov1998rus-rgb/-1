#!/usr/bin/env python3
"""Полностью автоматический Telegram-канал о Формуле 1.

Что делает за один запуск (запускается по cron каждые 30 минут):
  1. Новости: берёт свежие статьи из RSS, переводит на русский, постит (не более 3 за раз).
  2. Анонс уикенда: за ~3 дня до первой сессии публикует расписание.
  3. Напоминание: за ~1.5 часа до старта гонки.
  4. Итоги гонки + личный зачёт и кубок конструкторов после финиша.

Данные о календаре и результатах: Jolpica F1 API (бывший Ergast).
"""
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
from deep_translator import GoogleTranslator

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]  # например @my_f1_channel
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
TZ_LABEL = os.getenv("TIMEZONE_LABEL", "МСК")

STATE_FILE = Path(__file__).with_name("state.json")
API = "https://api.jolpi.ca/ergast/f1"
UA = "Mozilla/5.0 (compatible; f1-channel-bot/1.0)"

FEEDS = [
    "https://www.motorsport.com/rss/f1/news/",
    "https://www.autosport.com/rss/f1/news/",
    "https://feeds.bbci.co.uk/sport/formula1/rss.xml",
    "https://www.formula1.com/content/fom-website/en/latest/all.xml",
]

MAX_NEWS_PER_RUN = 3
MAX_NEWS_AGE = timedelta(hours=36)
PREVIEW_BEFORE = timedelta(days=3)
REMINDER_BEFORE = timedelta(minutes=90)

SESSION_NAMES = [
    ("FirstPractice", "Свободная практика 1"),
    ("SprintQualifying", "Спринт-квалификация"),
    ("SecondPractice", "Свободная практика 2"),
    ("Sprint", "Спринт"),
    ("ThirdPractice", "Свободная практика 3"),
    ("Qualifying", "Квалификация"),
]
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ---------- утилиты ----------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


def esc(s):
    return html.escape(s, quote=False)


def tr(text):
    text = (text or "").strip()
    if not text:
        return ""
    try:
        return GoogleTranslator(source="en", target="ru").translate(text[:4500]) or text
    except Exception as e:
        print("translate failed:", e)
        return text


def send(text, preview=False):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHANNEL_ID,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
        },
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram error: {r.status_code} {r.text}")


def fmt_dt(d):
    d = d.astimezone(TZ)
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m} {d:%H:%M}"


def parse_dt(block):
    t = block.get("time", "00:00:00Z")
    return datetime.fromisoformat(f"{block['date']}T{t}".replace("Z", "+00:00"))


# ---------- новости ----------

def clean_summary(raw, limit=350):
    s = re.sub(r"<[^>]+>", " ", raw or "")
    s = re.sub(r"\s+", " ", html.unescape(s)).strip()
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + "…"


def fetch_news():
    items = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": UA})
        except Exception as e:
            print("feed failed:", url, e)
            continue
        for e in feed.entries:
            link, title = e.get("link"), e.get("title")
            if not link or not title:
                continue
            ts = e.get("published_parsed") or e.get("updated_parsed")
            published = (
                datetime(*ts[:6], tzinfo=timezone.utc) if ts else datetime.now(timezone.utc)
            )
            items.append(
                {
                    "link": link,
                    "title": title.strip(),
                    "summary": clean_summary(e.get("summary", "")),
                    "published": published,
                    "source": urlparse(link).netloc.replace("www.", ""),
                }
            )
    items.sort(key=lambda x: x["published"], reverse=True)
    return items


def task_news(state):
    items = fetch_news()
    if not items:
        print("no news fetched")
        return
    first_run = "seen" not in state
    seen = list(state.get("seen", []))
    seen_set = set(seen)
    now = datetime.now(timezone.utc)

    candidates, titles = [], set()
    for it in items:
        if it["link"] in seen_set:
            continue
        norm = re.sub(r"\W+", " ", it["title"].lower()).strip()
        if norm in titles or now - it["published"] > MAX_NEWS_AGE:
            continue
        titles.add(norm)
        candidates.append(it)

    limit = 2 if first_run else MAX_NEWS_PER_RUN
    to_post = candidates[:limit][::-1]  # сначала самые старые
    posting = {it["link"] for it in to_post}

    # всё остальное считаем просмотренным, чтобы не постить «хвосты»
    for it in items:
        if it["link"] not in posting and it["link"] not in seen_set:
            seen.append(it["link"])
            seen_set.add(it["link"])

    for it in to_post:
        title = tr(it["title"])
        summary = tr(it["summary"]) if it["summary"] else ""
        text = f"🏎 <b>{esc(title)}</b>"
        if summary:
            text += f"\n\n{esc(summary)}"
        text += f'\n\n<a href="{html.escape(it["link"])}">Источник: {esc(it["source"])}</a>'
        send(text, preview=True)
        seen.append(it["link"])
        print("posted news:", it["title"])

    state["seen"] = seen[-1000:]


# ---------- календарь ----------

def load_races():
    r = requests.get(f"{API}/current.json", timeout=30, headers={"User-Agent": UA})
    r.raise_for_status()
    return r.json()["MRData"]["RaceTable"]["Races"]


def race_sessions(race):
    out = [(name, parse_dt(race[key])) for key, name in SESSION_NAMES if key in race]
    out.append(("Гонка", parse_dt(race)))
    return sorted(out, key=lambda x: x[1])


def race_title(race):
    return tr(race["raceName"])


def task_schedule(state):
    now = datetime.now(timezone.utc)
    races = load_races()
    upcoming = next((r for r in races if parse_dt(r) > now), None)
    if not upcoming:
        print("no upcoming races")
        return
    key = f"{upcoming['season']}-{upcoming['round']}"
    sessions = race_sessions(upcoming)
    first_start, race_start = sessions[0][1], sessions[-1][1]

    # анонс уикенда
    previews = state.setdefault("preview", [])
    if key not in previews and first_start - now <= PREVIEW_BEFORE:
        c = upcoming["Circuit"]
        place = tr(f"{c['circuitName']}, {c['Location']['locality']}, {c['Location']['country']}")
        lines = [f"🏁 <b>{esc(race_title(upcoming))} — уикенд уже скоро!</b>"]
        lines.append(f"📍 {esc(place)}")
        lines.append(f"🔢 Этап {upcoming['round']} сезона {upcoming['season']}")
        lines.append(f"\n🗓 <b>Расписание ({TZ_LABEL}):</b>")
        for name, d in sessions:
            mark = "🏆" if name == "Гонка" else "▫️"
            lines.append(f"{mark} {fmt_dt(d)} — {name}")
        send("\n".join(lines))
        previews.append(key)
        print("posted preview", key)

    # напоминание о старте гонки
    reminders = state.setdefault("reminder", [])
    if key not in reminders and timedelta(0) < race_start - now <= REMINDER_BEFORE:
        local = race_start.astimezone(TZ)
        send(
            f"🚦 <b>{esc(race_title(upcoming))}</b> — старт гонки сегодня в "
            f"<b>{local:%H:%M} {TZ_LABEL}</b>!\nСкоро погаснут красные огни 🔴🔴🔴🔴🔴"
        )
        reminders.append(key)
        print("posted reminder", key)


# ---------- результаты ----------

def task_results(state):
    r = requests.get(f"{API}/current/last/results.json", timeout=30, headers={"User-Agent": UA})
    r.raise_for_status()
    races = r.json()["MRData"]["RaceTable"]["Races"]
    if not races:
        return
    race = races[0]
    key = f"{race['season']}-{race['round']}"
    done = state.setdefault("results", [])
    if key in done:
        return
    if datetime.now(timezone.utc) - parse_dt(race) > timedelta(days=4):
        done.append(key)  # слишком старое — не постим
        return

    lines = [f"🏆 <b>Итоги: {esc(race_title(race))}</b>\n"]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for res in race["Results"][:10]:
        pos = int(res["position"])
        drv = res["Driver"]
        name = f"{drv['givenName']} {drv['familyName']}"
        gap = res.get("Time", {}).get("time") or res.get("status", "")
        lines.append(
            f"{medals.get(pos, str(pos) + '.')} {esc(name)} ({esc(res['Constructor']['name'])}) — {esc(gap)}"
        )

    try:
        ds = requests.get(f"{API}/current/driverStandings.json", timeout=30, headers={"User-Agent": UA})
        rows = ds.json()["MRData"]["StandingsTable"]["StandingsLists"][0]["DriverStandings"]
        lines.append("\n📊 <b>Личный зачёт (топ-10):</b>")
        for row in rows[:10]:
            d = row["Driver"]
            lines.append(f"{row['position']}. {esc(d['givenName'] + ' ' + d['familyName'])} — {row['points']}")
        cs = requests.get(f"{API}/current/constructorStandings.json", timeout=30, headers={"User-Agent": UA})
        rows = cs.json()["MRData"]["StandingsTable"]["StandingsLists"][0]["ConstructorStandings"]
        lines.append("\n🏭 <b>Кубок конструкторов:</b>")
        for row in rows[:10]:
            lines.append(f"{row['position']}. {esc(row['Constructor']['name'])} — {row['points']}")
    except Exception as e:
        print("standings failed:", e)

    send("\n".join(lines))
    done.append(key)
    print("posted results", key)


# ---------- запуск ----------

def main():
    state = load_state()
    errors = 0
    for name, fn in [("news", task_news), ("schedule", task_schedule), ("results", task_results)]:
        try:
            fn(state)
        except Exception as e:
            errors += 1
            print(f"[{name}] ERROR: {e}", file=sys.stderr)
        save_state(state)
    sys.exit(1 if errors == 3 else 0)


if __name__ == "__main__":
    main()
