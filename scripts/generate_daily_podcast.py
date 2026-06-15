#!/usr/bin/env python3
import asyncio
import datetime as dt
import html
import json
import re
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
        with urllib.request.urlopen(req, timeout=20) as response:
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


def content_hint(lang: str, item: dict) -> str:
    genre = ", ".join(item.get("genres") or [item.get("summary", "Podcast")])
    episode = item.get("latestEpisodeTitle", "")
    summary = item.get("latestEpisodeSummary", "")
    release = item.get("latestEpisodeDate") or item.get("releaseDate", "")
    if lang == "en":
        parts = [f"Public metadata places it around {genre}."]
        if episode:
            parts.append(f"The latest visible episode is titled {episode}.")
        if summary:
            parts.append(f"The public feed summary points toward this angle: {summary}")
        if release:
            parts.append(f"The visible update time is {release}.")
        return " ".join(parts)
    if lang == "zh":
        parts = [f"公开元数据里的类型线索是：{genre}。"]
        if episode:
            parts.append(f"最近可见单集标题是《{episode}》。")
        if summary:
            parts.append(f"公开 RSS 摘要透露的切入点是：{summary}")
        if release:
            parts.append(f"可见更新时间是 {release}。")
        return "".join(parts)
    parts = [f"公開メタデータ上のジャンルは {genre} です。"]
    if episode:
        parts.append(f"確認できる最新エピソードのタイトルは「{episode}」です。")
    if summary:
        parts.append(f"公開フィードの概要から見る論点は、{summary}")
    if release:
        parts.append(f"確認できる更新日は {release} です。")
    return "".join(parts)


def dialogue_for_item(lang: str, rank: int, item: dict) -> list[tuple[str, str]]:
    name = item["name"]
    artist = item["artist"]
    hint = content_hint(lang, item)
    if lang == "en":
        return [
            ("a", f"Pick {rank}: {name}, from {artist}. {hint}"),
            ("b", "What interests me is not only the topic, but the promise the show is making. A podcast has to earn your attention slowly. If the premise is clear in the first few minutes, it becomes much easier to stay with it."),
            ("a", "I partly agree, but I also think a strong premise can become a trap. Some shows are too optimized around a neat category. The better question is whether the host can keep finding tension inside the subject."),
            ("b", "Exactly. My listening test would be: does this episode leave me with a sharper question than the one I started with? If yes, it is worth a full listen."),
        ]
    if lang == "zh":
        return [
            ("a", f"第 {rank} 个节目是《{name}》，发布方是 {artist}。{hint}"),
            ("b", "我关心的是它为什么值得被听。播客不是标题党，真正留住人的往往是主持人能不能把一个普通话题讲出新的问题。"),
            ("a", "但我会稍微保留一点。很多节目光有问题意识还不够，如果对谈没有节奏，信息没有层次，听众很快会走神。"),
            ("b", "所以我的判断标准是：听完十分钟，我是不是更想继续追问？如果答案是，那它就不只是一个榜单条目，而是值得放进今天的收听队列。"),
        ]
    return [
        ("a", f"{rank} 本目は「{name}」、配信元は {artist} です。{hint}"),
        ("b", "私が気になるのは、テーマそのものよりも、その番組がどんな問いを立てているかです。良いポッドキャストは、情報を並べるだけではなく、考える余白を残します。"),
        ("a", "ただ、問いが良くても話し方が単調だと続きません。声の距離感、会話のテンポ、そして話題を深める順番がかなり大事です。"),
        ("b", "そうですね。最初の十分で、もっと聞きたいと思える問いが増えるかどうか。そこが今日のおすすめとして見るポイントです。"),
    ]


def build_dialogue(lang: str, items: list[dict]) -> list[tuple[str, str]]:
    if lang == "en":
        lines = [
            ("a", "Welcome to the English section. Today we are choosing only three podcasts, so we can slow down and actually discuss them."),
            ("b", "And just to be clear, we are using public Apple Podcasts metadata and public feed notes. This is a listening guide, not a replacement for the original shows."),
        ]
    elif lang == "zh":
        lines = [
            ("a", "现在进入中文部分。今天每种语言只选三个节目，我们不再报菜名，而是认真聊一聊它们为什么值得听。"),
            ("b", "先说明一下：这里依据的是 Apple Podcasts 公开元数据和公开 RSS 信息，不复述长段原节目内容，只做收听指南和观点延展。"),
        ]
    else:
        lines = [
            ("a", "ここからは日本語セクションです。今日は三つの番組だけを選び、少し深く話していきます。"),
            ("b", "Apple Podcasts と公開 RSS の情報をもとにした聞きどころの整理です。元の番組の代わりではありません。"),
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
    for index, (speaker, text) in enumerate(dialogue, start=1):
        part = prefix.with_name(f"{prefix.name}-{index:02d}-{speaker}.mp3")
        await synthesize(text, voices[speaker], part)
        parts.append(part)
    return parts


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
        sources[lang] = trim_and_enrich(sources[lang])

    parts = []
    for lang in ("en", "zh", "ja"):
        dialogue = build_dialogue(lang, sources[lang])
        parts.extend(await synthesize_dialogue(dialogue, VOICES[lang], EPISODES_DIR / f"{date.isoformat()}-{lang}"))

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
