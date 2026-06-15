#!/usr/bin/env python3
import asyncio
import datetime as dt
import html
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import edge_tts


SITE_URL = "https://jianjianpanpan.github.io/dailypodcast"
ROOT = Path(__file__).resolve().parents[1]
EPISODES_DIR = ROOT / "episodes"
DATA_DIR = ROOT / "data"

VOICES = {
    "en": "en-US-BrianNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "ja": "ja-JP-NanamiNeural",
}

CHARTS = {
    "en": {
        "label": "English",
        "country": "us",
        "search_term": "podcast",
        "lang_name": "English",
        "intro": "Here are today's English-language Apple Podcasts signals.",
    },
    "zh": {
        "label": "中文",
        "country": "cn",
        "search_term": "播客",
        "lang_name": "中文",
        "intro": "这里是今天中文播客的 Apple Podcasts 公开榜单观察。",
    },
    "ja": {
        "label": "日本語",
        "country": "jp",
        "search_term": "ポッドキャスト",
        "lang_name": "日本語",
        "intro": "今日の日本語ポッドキャストの公開ランキングから、注目番組を紹介します。",
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


def normalize_search_item(item: dict) -> dict:
    return {
        "name": item.get("collectionName") or item.get("trackName") or "Untitled podcast",
        "artist": item.get("artistName", "Unknown publisher"),
        "url": item.get("collectionViewUrl") or item.get("trackViewUrl") or "",
        "summary": item.get("primaryGenreName", "Podcast"),
        "releaseDate": item.get("releaseDate", ""),
        "genres": item.get("genres", []),
        "sourceMode": "itunes-search",
    }


def fetch_search(country: str, term: str, limit: int = 10) -> list[dict]:
    url = (
        "https://itunes.apple.com/search"
        f"?media=podcast&entity=podcast&country={country}&term={urllib.parse.quote(term)}&limit={limit}"
    )
    payload = fetch_json(url)
    results = payload.get("results", [])
    return [normalize_search_item(item) for item in results[:limit]]


def fetch_chart(country: str, limit: int = 10) -> list[dict]:
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
                        "sourceMode": "apple-marketing-chart",
                    }
                    for item in results[:limit]
                ]
        except Exception as exc:  # keep the daily job resilient
            last_error = str(exc)
    raise RuntimeError(last_error or "chart unavailable")


def sentence_for(lang: str, rank: int, item: dict) -> str:
    name = item["name"]
    artist = item["artist"]
    genre = ", ".join(item.get("genres") or [item.get("summary", "Podcast")])
    release = item.get("releaseDate", "")
    if lang == "en":
        return (
            f"Number {rank}: {name}, from {artist}. "
            f"The public metadata places it around {genre}. "
            + (f"The latest visible update is {release}. " if release else "")
            + "The listening cue here is simple: this is a show worth sampling if you want to understand what Apple Podcasts is surfacing in this language market. "
            "We are not replaying the original program or quoting it at length. We are using public metadata to explain why the show belongs in today's listening queue. "
            "If you open the original Apple Podcasts link, pay attention to three things: the host's pace, how quickly the episode establishes context, and whether the show feels useful enough to subscribe to after one episode."
        )
    if lang == "zh":
        return (
            f"第 {rank} 个节目是《{name}》，发布方是 {artist}。"
            f"公开元数据里能看到的类型线索是：{genre}。"
            + (f"可见更新时间是 {release}。" if release else "")
            + "今天把它放进简报，不是为了复述节目原文，而是为了提示它在 Apple Podcasts 公开信息里有可观察的热度或相关性。"
            "你真正打开原节目时，可以重点听三个点：主持人是不是快速建立语境，话题有没有持续展开的价值，以及它是否适合加入长期订阅列表。"
        )
    return (
        f"{rank} 位は「{name}」、配信元は {artist} です。"
        f"公開メタデータ上のジャンルは {genre} です。"
        + (f"確認できる更新日は {release} です。" if release else "")
        + "この紹介は番組内容の代替ではなく、Apple Podcasts で見える公開情報にもとづく短い聞きどころの整理です。"
        "実際に聴くときは、導入のわかりやすさ、話題の深さ、そして継続して聴きたい声かどうかに注目するとよいでしょう。"
    )


def build_segment(lang: str, items: list[dict]) -> str:
    intro = CHARTS[lang]["intro"]
    lines = [intro]
    for index, item in enumerate(items, start=1):
        lines.append(sentence_for(lang, index, item))
    if lang == "en":
        lines.append("That is the English section. Next, we move to Chinese podcasts.")
    elif lang == "zh":
        lines.append("以上是中文部分。接下来进入日文播客观察。")
    else:
        lines.append("以上、日本語セクションでした。今日の多言語ポッドキャスト速報はここまでです。")
    return "\n".join(lines)


async def synthesize(text: str, voice: str, output: Path) -> None:
    communicate = edge_tts.Communicate(text=text, voice=voice, rate="-5%")
    await communicate.save(str(output))


def concat_mp3(parts: list[Path], output: Path) -> None:
    # MP3 frame concatenation is accepted by browsers for these Edge TTS outputs.
    with output.open("wb") as out:
        for part in parts:
            out.write(part.read_bytes())


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
      <p>最新一期：{date.isoformat()}。中英日多语言 Apple Podcasts 公开榜单音频摘要。音频由 AI 生成。</p>
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

    sources: dict[str, list[dict]] = {}
    for lang, config in CHARTS.items():
        try:
            sources[lang] = fetch_chart(config["country"])
        except Exception:
            try:
                sources[lang] = fetch_search(config["country"], config["search_term"])
            except Exception:
                sources[lang] = FALLBACK_ITEMS[lang]

    parts = []
    for lang in ("en", "zh", "ja"):
        text = build_segment(lang, sources[lang])
        part = EPISODES_DIR / f"{date.isoformat()}-{lang}.mp3"
        await synthesize(text, VOICES[lang], part)
        parts.append(part)

    episode_name = f"{date.isoformat()}.mp3"
    episode_path = EPISODES_DIR / episode_name
    concat_mp3(parts, episode_path)

    for part in parts:
        part.unlink(missing_ok=True)

    episode_file = f"episodes/{episode_name}"
    write_index(date, episode_file, sources)
    write_metadata(date, episode_file, sources)

    print(f"Generated {episode_path}")
    print(f"Published URL: {SITE_URL}/{episode_file}")


if __name__ == "__main__":
    asyncio.run(main())
