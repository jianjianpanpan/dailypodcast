#!/usr/bin/env python3
import asyncio
import datetime as dt
import html
import json
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import edge_tts


SITE_URL = "https://jianjianpanpan.github.io/dailypodcast"
ROOT = Path(__file__).resolve().parents[1]
EPISODES_DIR = ROOT / "episodes"
DATA_DIR = ROOT / "data"

VOICES = {
    "en": {
        "a": "en-US-BrianNeural",
        "b": "en-US-AvaNeural",
    },
    "zh": {
        "a": "zh-CN-YunxiNeural",
        "b": "zh-CN-XiaoxiaoNeural",
    },
    "ja": {
        "a": "ja-JP-KeitaNeural",
        "b": "ja-JP-NanamiNeural",
    },
}

ITEMS_PER_LANGUAGE = 3
WUXI_LATITUDE = 31.4912
WUXI_LONGITUDE = 120.3119

CHINESE_WEEKDAYS = "一二三四五六日"
WEATHER_CODES_ZH = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "有雾",
    48: "有雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "较强毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "短时小阵雨",
    81: "阵雨",
    82: "强阵雨",
    95: "雷雨",
    96: "雷雨伴小冰雹",
    99: "雷雨伴冰雹",
}

CHARTS = {
    "en": {
        "label": "English",
        "country": "us",
        "search_term": "podcast",
        "lang_name": "English",
    },
    "zh": {
        "label": "中文",
        "country": "cn",
        "search_term": "播客",
        "lang_name": "中文",
    },
    "ja": {
        "label": "日本語",
        "country": "jp",
        "search_term": "ポッドキャスト",
        "lang_name": "日本語",
    },
}

FALLBACK_ITEMS = {
    "en": [
        {
            "name": "Apple Podcasts US Top Podcasts",
            "artist": "Apple Podcasts",
            "url": "https://podcasts.apple.com/us/charts",
            "summary": "The public chart was unavailable during generation, so this segment explains the data limitation and keeps the format ready for tomorrow.",
        }
    ],
    "zh": [
        {
            "name": "Apple Podcasts 中国区播客榜单",
            "artist": "Apple Podcasts",
            "url": "https://podcasts.apple.com/cn/charts",
            "summary": "生成时无法读取公开榜单数据，因此本段保留数据说明，并等待下一次自动更新。",
        }
    ],
    "ja": [
        {
            "name": "Apple Podcasts 日本ランキング",
            "artist": "Apple Podcasts",
            "url": "https://podcasts.apple.com/jp/charts",
            "summary": "生成時に公開ランキングを取得できなかったため、このセクションではデータ制限を説明します。",
        }
    ],
}


def today_shanghai() -> dt.date:
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()


def date_intro(date: dt.date) -> str:
    weekday = CHINESE_WEEKDAYS[date.weekday()]
    return f"今天是 {date.month} 月 {date.day} 日，星期{weekday}。"


def fetch_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "daily-podcast-bot/1.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
    if not body.strip():
        raise ValueError("empty response")
    return json.loads(body.decode("utf-8"))


def fetch_wuxi_weather() -> dict:
    query = urllib.parse.urlencode(
        {
            "latitude": WUXI_LATITUDE,
            "longitude": WUXI_LONGITUDE,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "timezone": "Asia/Shanghai",
        }
    )
    return fetch_json(f"https://api.open-meteo.com/v1/forecast?{query}")


def weather_intro() -> str:
    try:
        current = fetch_wuxi_weather().get("current", {})
        temp = current.get("temperature_2m")
        feels = current.get("apparent_temperature")
        humidity = current.get("relative_humidity_2m")
        wind = current.get("wind_speed_10m")
        code = current.get("weather_code")
        condition = WEATHER_CODES_ZH.get(code, "天气情况待确认")
        return (
            f"无锡现在{condition}，气温约 {temp:.0f} 度，体感约 {feels:.0f} 度，"
            f"湿度 {humidity:.0f}%，风速约 {wind:.0f} 公里每小时。"
        )
    except Exception:
        return "无锡天气暂时没有取到实时数据，出门前可以再看一眼本地天气。"


def normalize_search_item(item: dict) -> dict:
    return {
        "name": item.get("collectionName") or item.get("trackName") or "Untitled podcast",
        "artist": item.get("artistName", "Unknown publisher"),
        "url": item.get("collectionViewUrl") or item.get("trackViewUrl") or "",
        "summary": item.get("primaryGenreName", "Podcast"),
        "releaseDate": item.get("releaseDate", ""),
        "genres": item.get("genres", []),
        "feedUrl": item.get("feedUrl", ""),
        "sourceMode": "itunes-search",
    }


def fetch_search(country: str, term: str, limit: int = 8) -> list[dict]:
    url = (
        "https://itunes.apple.com/search"
        f"?media=podcast&entity=podcast&country={country}&term={urllib.parse.quote(term)}&limit={limit}"
    )
    payload = fetch_json(url)
    results = payload.get("results", [])
    return [normalize_search_item(item) for item in results[:limit]]


def fetch_chart(country: str, limit: int = 8) -> list[dict]:
    urls = [
        f"https://rss.applemarketingtools.com/api/v2/{country}/podcasts/top-podcasts/all/{limit}/explicit.json",
        f"https://rss.marketingtools.apple.com/api/v2/{country}/podcasts/top-podcasts/all/{limit}/explicit.json",
    ]
    last_error = None
    for url in urls:
        try:
            payload = fetch_json(url)
            results = payload.get("feed", {}).get("results", [])
            if results:
                return [
                    {
                        "name": item.get("name", "Untitled podcast"),
                        "artist": item.get("artistName", "Unknown publisher"),
                        "url": item.get("url", ""),
                        "summary": item.get("name", "A public Apple Podcasts chart entry."),
                        "releaseDate": item.get("releaseDate", ""),
                        "genres": item.get("genres", []),
                        "feedUrl": item.get("feedUrl", ""),
                        "sourceMode": "apple-marketing-chart",
                    }
                    for item in results[:limit]
                ]
        except Exception as exc:  # keep the daily job resilient
            last_error = str(exc)
    raise RuntimeError(last_error or "chart unavailable")


def strip_markup(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def brief(value: str, limit: int = 150) -> str:
    value = strip_markup(value)
    if len(value) <= limit:
        return value
    return value[:limit].rsplit(" ", 1)[0].strip() + "..."


def enrich_latest_episode(item: dict) -> dict:
    feed_url = item.get("feedUrl")
    if not feed_url:
        return item
    try:
        req = urllib.request.Request(feed_url, headers={"User-Agent": "daily-podcast-bot/1.0"})
        with urllib.request.urlopen(req, timeout=8) as response:
            root = ET.fromstring(response.read())
        channel = root.find("channel")
        episode = channel.find("item") if channel is not None else root.find(".//item")
        if episode is None:
            return item
        title = strip_markup(episode.findtext("title", ""))
        description = (
            episode.findtext("description", "")
            or episode.findtext("{http://www.itunes.com/dtds/podcast-1.0.dtd}summary", "")
        )
        pub_date = strip_markup(episode.findtext("pubDate", ""))
        item = dict(item)
        item["latestEpisodeTitle"] = title
        item["latestEpisodeSummary"] = brief(description, 180)
        item["latestEpisodeDate"] = pub_date
    except Exception:
        return item
    return item


def trim_and_enrich(items: list[dict]) -> list[dict]:
    return [enrich_latest_episode(item) for item in items[:ITEMS_PER_LANGUAGE]]


def topic_hint(lang: str, item: dict) -> str:
    genre = ", ".join(item.get("genres") or [item.get("summary", "Podcast")])
    episode = item.get("latestEpisodeTitle", "")
    summary = item.get("latestEpisodeSummary", "")
    if lang == "en":
        topic = episode or item.get("summary") or genre
        parts = [f"The show is called {item['name']}, and the visible topic today is: {topic}."]
        if episode:
            parts.append(f"That title suggests a conversation about {episode}.")
        if summary:
            parts.append(f"The public feed summary adds this clue: {summary}")
        parts.append(f"The broader lane is {genre}.")
        return " ".join(parts)
    if lang == "zh":
        topic = episode or item.get("summary") or genre
        parts = [f"这个播客叫《{item['name']}》，今天能看到的话题线索是：{topic}。"]
        if episode:
            parts.append(f"从标题看，它讨论的不是一个孤立信息点，而是“{episode}”背后的经验和判断。")
        if summary:
            parts.append(f"公开摘要给出的切入点是：{summary}")
        parts.append(f"它大致落在 {genre} 这个方向。")
        return "".join(parts)
    topic = episode or item.get("summary") or genre
    parts = [f"このポッドキャストは「{item['name']}」。見えている話題は「{topic}」です。"]
    if episode:
        parts.append(f"タイトルから見ると、「{episode}」について考える回だと言えます。")
    if summary:
        parts.append(f"公開フィードの概要では、{summary}")
    parts.append(f"大きなジャンルは {genre} です。")
    return "".join(parts)


def dialogue_for_item(lang: str, rank: int, item: dict) -> list[tuple[str, str]]:
    name = item["name"]
    artist = item["artist"]
    hint = topic_hint(lang, item)
    if lang == "en":
        return [
            ("a", f"First, let's talk about {name}, from {artist}. {hint}"),
            ("b", "What I like about that premise is that it gives us a doorway into something larger. At thirty-five, you start to notice that the useful conversations are rarely just about the headline. They are about habits, incentives, memory, status, or the way people make choices under pressure."),
            ("a", "Right, and I would push it a little further. A good podcast topic should help you test your own assumptions. My listening test is simple: does it give us one sharper way to talk about the subject tomorrow?"),
        ]
    if lang == "zh":
        return [
            ("a", f"先聊《{name}》，发布方是 {artist}。{hint}"),
            ("b", "我喜欢从这种题目里看一个更大的问题：它表面上是在聊一个节目或者一个事件，但真正吸引人的，往往是背后的生活经验、社会情绪，或者一代人正在形成的新常识。"),
            ("a", "对，而且三十五岁以后再听播客，我会更在意它有没有判断力。不是观点越猛越好，而是它能不能把复杂事情拆开，让你听完之后多一个角度，而不是只多知道一个标题。"),
        ]
    return [
        ("a", f"まず「{name}」です。配信元は {artist}。{hint}"),
        ("b", "このテーマで面白いのは、単なる情報ではなく、その奥にある価値観が見えてくるところだと思います。三十五歳くらいになると、話題そのものよりも、人がなぜそう考えるのかに興味が移ってきます。"),
        ("a", "そうですね。ただ、雰囲気だけで深そうに聞こえる番組もあります。大事なのは、具体的な場面や反対側の見方まで出てくるかどうか。この番組を聴くなら、どんな問いが残るかに注目したいですね。"),
    ]


def build_opening(date: dt.date) -> list[tuple[str, str]]:
    return [
        ("a", f"早上好。{date_intro(date)}{weather_intro()}"),
        ("b", "今天我们继续做一档中英日混合的播客晨间听单。每种语言只选三个节目，不追求信息堆满，而是把它们当作话题入口，聊聊它们背后真正值得想的东西。"),
    ]


def build_dialogue(lang: str, items: list[dict]) -> list[tuple[str, str]]:
    if lang == "en":
        lines = [
            ("a", "Let's move into the English section. Three podcasts, three topics, and a little room to think around them."),
            ("b", "Good. Less catalog, more conversation. We are using public Apple Podcasts and feed information, then adding our own reading of why the topic matters."),
        ]
    elif lang == "zh":
        lines = [
            ("a", "现在进入中文部分。三个节目，三个话题，我们尽量聊得像朋友早上坐下来交换判断。"),
            ("b", "对，不做资料朗读。我们会说它大概讨论什么，再顺着这个话题展开一点自己的看法。"),
        ]
    else:
        lines = [
            ("a", "ここからは日本語セクションです。三つの番組を入り口にして、少し考えを広げていきます。"),
            ("b", "番組の細かい情報を読むのではなく、そこで扱われているテーマをどう受け取れるかを話していきます。"),
        ]
    for index, item in enumerate(items, start=1):
        lines.extend(dialogue_for_item(lang, index, item))
    if lang == "en":
        lines.append(("a", "That is the English section. Next, we move to Chinese podcasts."))
    elif lang == "zh":
        lines.append(("a", "以上是中文部分。接下来进入日文播客观察。"))
    else:
        lines.append(("a", "以上、日本語セクションでした。今日の多言語ポッドキャスト速報はここまでです。"))
    return lines


async def synthesize(text: str, voice: str, output: Path) -> None:
    communicate = edge_tts.Communicate(text=text, voice=voice, rate="-5%")
    await communicate.save(str(output))


async def synthesize_dialogue(dialogue: list[tuple[str, str]], voices: dict, prefix: Path) -> list[Path]:
    parts = []
    compacted = []
    for speaker, text in dialogue:
        if compacted and compacted[-1][0] == speaker:
            compacted[-1] = (speaker, compacted[-1][1] + "\n" + text)
        else:
            compacted.append((speaker, text))
    for index, (speaker, text) in enumerate(compacted, start=1):
        part = prefix.with_name(f"{prefix.name}-{index:02d}-{speaker}.mp3")
        await synthesize(text, voices[speaker], part)
        parts.append(part)
    return parts


def run_checked(args: list[str]) -> None:
    subprocess.run(args, check=True, cwd=ROOT)


def concat_mp3(parts: list[Path], output: Path) -> None:
    # MP3 frame concatenation is accepted by browsers for these Edge TTS outputs.
    with output.open("wb") as out:
        for part in parts:
            out.write(part.read_bytes())


def cleanup_partial_files(date: dt.date) -> None:
    for path in EPISODES_DIR.glob(f"{date.isoformat()}-*.mp3"):
        path.unlink(missing_ok=True)


def add_background_music(speech_path: Path, output_path: Path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        if speech_path != output_path:
            output_path.write_bytes(speech_path.read_bytes())
        return

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(speech_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    duration = max(float(probe.stdout.strip()), 1.0)
    music_filter = (
        f"sine=frequency=174:duration={duration}:sample_rate=24000,"
        "volume=0.018,afade=t=in:st=0:d=3,"
        f"afade=t=out:st={max(duration - 4, 0)}:d=4[m]"
    )
    run_checked(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(speech_path),
            "-filter_complex",
            music_filter + ";[0:a]volume=1.0[v];[v][m]amix=inputs=2:duration=first:dropout_transition=2",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "96k",
            str(output_path),
        ]
    )


def write_index(date: dt.date, episode_file: str, sources: dict) -> None:
    rows = []
    for lang, items in sources.items():
        for index, item in enumerate(items, start=1):
            rows.append(
                "<tr>"
                f"<td>{html.escape(CHARTS[lang]['label'])}</td>"
                f"<td>{index}</td>"
                f"<td><a href=\"{html.escape(item.get('url', ''))}\">{html.escape(item['name'])}</a></td>"
                f"<td>{html.escape(item['artist'])}</td>"
                "</tr>"
            )

    index_path = ROOT / "index.html"
    index_path.write_text(
        f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Daily Podcast</title>
    <style>
      body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f8fa; color: #111; }}
      main {{ max-width: 860px; margin: 0 auto; padding: 40px 18px; }}
      h1 {{ margin: 0 0 8px; font-size: 30px; }}
      p {{ line-height: 1.6; }}
      audio {{ width: 100%; margin: 18px 0 28px; }}
      table {{ width: 100%; border-collapse: collapse; background: white; }}
      th, td {{ padding: 10px 12px; border-bottom: 1px solid #e8eaed; text-align: left; vertical-align: top; }}
      th {{ font-size: 13px; color: #555; }}
      a {{ color: #0b57d0; }}
    </style>
  </head>
  <body>
    <main>
      <h1>Daily Podcast</h1>
      <p>最新一期：{date.isoformat()}。中英日三语播客晨间对谈，每种语言精选 3 个节目，音频由 AI 生成。</p>
      <audio controls src="{html.escape(episode_file)}"></audio>
      <p><a href="{html.escape(episode_file)}">打开音频文件</a></p>
      <h2>本期来源</h2>
      <table>
        <thead><tr><th>语言</th><th>排名</th><th>节目</th><th>发布方</th></tr></thead>
        <tbody>
          {''.join(rows)}
        </tbody>
      </table>
    </main>
  </body>
</html>
""",
        encoding="utf-8",
    )


def write_metadata(date: dt.date, episode_file: str, sources: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"{date.isoformat()}.json").write_text(
        json.dumps(
            {
                "date": date.isoformat(),
                "episode": f"{SITE_URL}/{episode_file}",
                "sources": sources,
                "note": "Sources are Apple Podcasts public chart metadata where available; fallback entries are marked by their generic Apple chart URLs.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


async def main() -> None:
    date = today_shanghai()
    EPISODES_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
    cleanup_partial_files(date)

    sources: dict[str, list[dict]] = {}
    for lang, config in CHARTS.items():
        try:
            sources[lang] = fetch_chart(config["country"])
        except Exception:
            try:
                sources[lang] = fetch_search(config["country"], config["search_term"])
            except Exception:
                sources[lang] = FALLBACK_ITEMS[lang]
        sources[lang] = trim_and_enrich(sources[lang])

    parts = []
    parts.extend(await synthesize_dialogue(build_opening(date), VOICES["zh"], EPISODES_DIR / f"{date.isoformat()}-opening"))
    for lang in ("en", "zh", "ja"):
        dialogue = build_dialogue(lang, sources[lang])
        parts.extend(await synthesize_dialogue(dialogue, VOICES[lang], EPISODES_DIR / f"{date.isoformat()}-{lang}"))

    episode_name = f"{date.isoformat()}.mp3"
    episode_path = EPISODES_DIR / episode_name
    speech_path = EPISODES_DIR / f"{date.isoformat()}-speech.mp3"
    concat_mp3(parts, speech_path)
    add_background_music(speech_path, episode_path)

    for part in parts:
        part.unlink(missing_ok=True)
    speech_path.unlink(missing_ok=True)

    episode_file = f"episodes/{episode_name}"
    write_index(date, episode_file, sources)
    write_metadata(date, episode_file, sources)

    print(f"Generated {episode_path}")
    print(f"Published URL: {SITE_URL}/{episode_file}")


if __name__ == "__main__":
    asyncio.run(main())
