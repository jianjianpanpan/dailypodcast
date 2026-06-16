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

WUXI_LATITUDE = 31.4912
WUXI_LONGITUDE = 120.3119
CHINESE_WEEKDAYS = "一二三四五六日"

VOICES = {
    "opening": {"a": "zh-CN-YunxiNeural", "b": "zh-CN-XiaoxiaoNeural"},
    "en": {"a": "en-US-BrianNeural", "b": "en-US-AvaNeural"},
    "zh": {"a": "zh-CN-YunxiNeural", "b": "zh-CN-XiaoxiaoNeural"},
    "ja": {"a": "ja-JP-KeitaNeural", "b": "ja-JP-NanamiNeural"},
}

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


def today_shanghai() -> dt.date:
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()


def date_intro(date: dt.date) -> str:
    return f"今天是 {date.month} 月 {date.day} 日，星期{CHINESE_WEEKDAYS[date.weekday()]}。"


def request_text(url: str, timeout: int = 20, accept: str = "*/*", referer: str = "https://www.google.com/") -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 daily-news-podcast/1.0",
            "Accept": accept,
            "Referer": referer,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", "ignore")


def fetch_json(url: str, timeout: int = 20, referer: str = "https://www.google.com/") -> dict:
    return json.loads(request_text(url, timeout=timeout, accept="application/json,text/plain,*/*", referer=referer))


def strip_markup(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def brief(value: str, limit: int) -> str:
    value = strip_markup(value)
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."


def absolute_url(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href)


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
        condition = WEATHER_CODES_ZH.get(current.get("weather_code"), "天气情况待确认")
        return (
            f"无锡现在{condition}，气温约 {temp:.0f} 度，体感约 {feels:.0f} 度，"
            f"湿度 {humidity:.0f}%，风速约 {wind:.0f} 公里每小时。"
        )
    except Exception:
        return "无锡天气暂时没有取到实时数据，出门前可以再看一眼本地天气。"


def parse_rss(url: str, source: str, lang: str, limit: int) -> list[dict]:
    root = ET.fromstring(request_text(url, timeout=20, accept="application/rss+xml,text/xml,*/*"))
    items = []
    for item in root.findall(".//item"):
        title = strip_markup(item.findtext("title", ""))
        link = strip_markup(item.findtext("link", ""))
        description = strip_markup(item.findtext("description", ""))
        pub_date = strip_markup(item.findtext("pubDate", ""))
        if title and link:
            items.append(
                {
                    "source": source,
                    "lang": lang,
                    "title": title,
                    "url": link,
                    "summary": brief(description, 180),
                    "published": pub_date,
                }
            )
        if len(items) >= limit:
            break
    return items


def fetch_nyt() -> list[dict]:
    return parse_rss("https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", "The New York Times", "en", 2)


def fetch_yahoo_japan() -> list[dict]:
    return parse_rss("https://news.yahoo.co.jp/rss/topics/top-picks.xml", "Yahoo!ニュース", "ja", 2)


def fetch_zaobao() -> list[dict]:
    base = "https://www.zaobao.com.sg/"
    page = request_text(base, timeout=20, accept="text/html,*/*")
    seen = set()
    items = []
    for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
        href = html.unescape(match.group(1))
        title = strip_markup(match.group(2))
        if not title or len(title) < 8:
            continue
        if not ("/news/" in href or "/finance/" in href):
            continue
        if title in seen or title in {"新加坡", "国际", "中国", "东南亚", "体育"}:
            continue
        seen.add(title)
        items.append(
            {
                "source": "联合早报",
                "lang": "zh",
                "title": title,
                "url": absolute_url(base, href),
                "summary": "联合早报首页公开标题。",
                "published": "",
            }
        )
        if len(items) >= 2:
            break
    return items


def fetch_weibo() -> list[dict]:
    url = "https://weibo.com/ajax/side/hotSearch"
    payload = fetch_json(url, timeout=20, referer="https://weibo.com/")
    realtime = payload.get("data", {}).get("realtime", [])
    for item in realtime:
        if item.get("is_ad"):
            continue
        word = strip_markup(item.get("word") or item.get("note") or "")
        if not word:
            continue
        encoded = urllib.parse.quote(word)
        return [
            {
                "source": "微博热搜",
                "lang": "zh",
                "title": word,
                "url": f"https://s.weibo.com/weibo?q=%23{encoded}%23",
                "summary": item.get("category") or item.get("flag_desc") or "微博公开热搜话题。",
                "published": "",
            }
        ]
    return []


def fallback_news() -> list[dict]:
    return [
        {
            "source": "The New York Times",
            "lang": "en",
            "title": "Global markets watch geopolitical risk",
            "url": "https://www.nytimes.com/",
            "summary": "A fallback item used when public feeds are temporarily unavailable.",
            "published": "",
        },
        {
            "source": "The New York Times",
            "lang": "en",
            "title": "Policy debates shape the international agenda",
            "url": "https://www.nytimes.com/",
            "summary": "A fallback item used when public feeds are temporarily unavailable.",
            "published": "",
        },
        {
            "source": "联合早报",
            "lang": "zh",
            "title": "亚洲区域新闻持续受到关注",
            "url": "https://www.zaobao.com.sg/",
            "summary": "公开首页暂时不可用时的备用条目。",
            "published": "",
        },
        {
            "source": "联合早报",
            "lang": "zh",
            "title": "财经与社会议题成为中文读者焦点",
            "url": "https://www.zaobao.com.sg/",
            "summary": "公开首页暂时不可用时的备用条目。",
            "published": "",
        },
        {
            "source": "微博热搜",
            "lang": "zh",
            "title": "微博热搜数据暂时不可用",
            "url": "https://s.weibo.com/top/summary",
            "summary": "微博接口不可用时的备用条目。",
            "published": "",
        },
        {
            "source": "Yahoo!ニュース",
            "lang": "ja",
            "title": "主要ニュースの更新を確認中",
            "url": "https://news.yahoo.co.jp/",
            "summary": "公開RSSが一時的に利用できない場合の予備項目です。",
            "published": "",
        },
        {
            "source": "Yahoo!ニュース",
            "lang": "ja",
            "title": "社会と経済の動きに注目",
            "url": "https://news.yahoo.co.jp/",
            "summary": "公開RSSが一時的に利用できない場合の予備項目です。",
            "published": "",
        },
    ]


def gather_news() -> list[dict]:
    items = []
    for fetcher in (fetch_nyt, fetch_zaobao, fetch_weibo, fetch_yahoo_japan):
        try:
            items.extend(fetcher())
        except Exception as exc:
            print(f"Source failed: {fetcher.__name__}: {exc}")
    fallback = fallback_news()
    while len(items) < 7:
        items.append(fallback[len(items)])
    return items[:7]


def build_opening(date: dt.date) -> list[tuple[str, str]]:
    return [
        ("a", f"早上好。{date_intro(date)}{weather_intro()}"),
        ("b", "今天的节目改成新闻晨报。我们浏览纽约时报、联合早报、微博热搜和 Yahoo Japan 的公开信息，选七条热点新闻，用播客对谈的方式讲清楚它们为什么重要。"),
    ]


def english_dialogue(index: int, item: dict) -> list[tuple[str, str]]:
    title = item["title"]
    summary = item.get("summary") or "The public feed gives only a headline."
    return [
        ("a", f"Story {index}: {title}. The public summary says: {summary}"),
        ("b", "The core issue is not just what happened, but what changes if this story keeps developing. I would watch who gains room to move, who loses leverage, and what ordinary people will feel first. That is how a headline turns into a real signal."),
    ]


def chinese_dialogue(index: int, item: dict) -> list[tuple[str, str]]:
    title = item["title"]
    summary = item.get("summary") or "公开信息主要来自标题和热度线索。"
    return [
        ("a", f"第 {index} 条，{title}。公开线索显示：{summary}"),
        ("b", "这条新闻值得听，不只是因为它热，而是因为它背后有一个更大的问题：公众在关心什么、制度在回应什么，或者市场正在重新定价什么。今天先别把它看成孤立事件，后续要看相关方会不会改变行动，以及普通人会不会真的感受到影响。"),
    ]


def japanese_dialogue(index: int, item: dict) -> list[tuple[str, str]]:
    title = item["title"]
    summary = item.get("summary") or "公開RSSでは見出しが中心です。"
    return [
        ("a", f"{index} 本目のニュースです。「{title}」。公開情報では、{summary}"),
        ("b", "大事なのは、出来事そのものだけではなく、それが社会のどこに波及するかです。政治、生活、企業行動のどれに影響するのかを見ると、ニュースの意味が立体的になります。結論を急がず、次の反応を追いたいですね。"),
    ]


def build_news_dialogue(items: list[dict]) -> list[tuple[str, str]]:
    dialogue = []
    for index, item in enumerate(items, start=1):
        if item["lang"] == "en":
            dialogue.extend(english_dialogue(index, item))
        elif item["lang"] == "ja":
            dialogue.extend(japanese_dialogue(index, item))
        else:
            dialogue.extend(chinese_dialogue(index, item))
    dialogue.append(("a", "以上就是今天的七条新闻。我们明天早上继续，用更少的标题，讲更清楚的变化。"))
    return dialogue


async def synthesize(text: str, voice: str, output: Path) -> None:
    last_error = None
    for attempt in range(3):
        try:
            communicate = edge_tts.Communicate(text=text, voice=voice, rate="-5%")
            await communicate.save(str(output))
            return
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(2 + attempt * 2)
    raise last_error


async def synthesize_dialogue(dialogue: list[tuple[str, str]], prefix: Path) -> list[Path]:
    parts = []
    for index, (speaker, text) in enumerate(dialogue, start=1):
        if text and re.search(r"[\u3040-\u30ff]", text):
            voices = VOICES["ja"]
        elif text and re.search(r"[\u4e00-\u9fff]", text):
            voices = VOICES["zh"]
        else:
            voices = VOICES["en"]
        part = prefix.with_name(f"{prefix.name}-{index:02d}-{speaker}.mp3")
        await synthesize(text, voices[speaker], part)
        parts.append(part)
    return parts


def concat_mp3(parts: list[Path], output: Path) -> None:
    with output.open("wb") as out:
        for part in parts:
            out.write(part.read_bytes())


def cleanup_partial_files(date: dt.date) -> None:
    patterns = [
        f"{date.isoformat()}-news-*.mp3",
        f"{date.isoformat()}-opening-*.mp3",
        f"{date.isoformat()}-speech.mp3",
    ]
    for pattern in patterns:
        for path in EPISODES_DIR.glob(pattern):
            path.unlink(missing_ok=True)


def run_checked(args: list[str]) -> None:
    subprocess.run(args, check=True, cwd=ROOT)


def add_background_music(speech_path: Path, output_path: Path) -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
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
        "volume=0.014,afade=t=in:st=0:d=3,"
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


def write_index(date: dt.date, episode_file: str, items: list[dict]) -> None:
    rows = []
    for index, item in enumerate(items, start=1):
        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{html.escape(item['source'])}</td>"
            f"<td>{html.escape({'en': 'English', 'zh': '中文', 'ja': '日本語'}[item['lang']])}</td>"
            f"<td><a href=\"{html.escape(item['url'])}\">{html.escape(item['title'])}</a></td>"
            "</tr>"
        )

    (ROOT / "index.html").write_text(
        f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Daily News Podcast</title>
    <style>
      body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f8fa; color: #111; }}
      main {{ max-width: 900px; margin: 0 auto; padding: 40px 18px; }}
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
      <h1>Daily News Podcast</h1>
      <p>最新一期：{date.isoformat()}。纽约时报、联合早报、微博热搜和 Yahoo Japan 七条热点新闻晨间播客。音频由 AI 生成，摘要基于公开标题、RSS 和公开热榜信息。</p>
      <audio controls src="{html.escape(episode_file)}"></audio>
      <p><a href="{html.escape(episode_file)}">打开音频文件</a></p>
      <h2>本期新闻来源</h2>
      <table>
        <thead><tr><th>#</th><th>来源</th><th>语言</th><th>新闻</th></tr></thead>
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


def write_metadata(date: dt.date, episode_file: str, items: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"{date.isoformat()}.json").write_text(
        json.dumps(
            {
                "date": date.isoformat(),
                "episode": f"{SITE_URL}/{episode_file}",
                "items": items,
                "note": "Summaries are original podcast-style commentary based on public RSS, public headlines, and public hot-search metadata. No full articles are reproduced.",
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

    items = gather_news()
    dialogue = build_opening(date) + build_news_dialogue(items)
    parts = await synthesize_dialogue(dialogue, EPISODES_DIR / f"{date.isoformat()}-news")

    episode_name = f"{date.isoformat()}.mp3"
    episode_path = EPISODES_DIR / episode_name
    speech_path = EPISODES_DIR / f"{date.isoformat()}-speech.mp3"
    concat_mp3(parts, speech_path)
    add_background_music(speech_path, episode_path)

    for part in parts:
        part.unlink(missing_ok=True)
    speech_path.unlink(missing_ok=True)

    episode_file = f"episodes/{episode_name}"
    write_index(date, episode_file, items)
    write_metadata(date, episode_file, items)

    print(f"Generated {episode_path}")
    print(f"Published URL: {SITE_URL}/{episode_file}")


if __name__ == "__main__":
    asyncio.run(main())
