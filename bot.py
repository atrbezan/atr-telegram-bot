"""
Instagram to Telegram Auto Forwarder Bot (Ultra-Resilient Standalone Version)
Features:
- Multi-engine scraping:
    1. Apify API (Residential Proxies - 100% bypass of datacenter 429 blocks)
    2. Authenticated Instagram Session (INSTAGRAM_SESSION_ID)
    3. Public embed crawler + yt-dlp fallback
- Telegram Bot API client supporting Reels/Videos, Photos, and Albums
- Automatic bot token & channel permission verification at startup
- Automatic caption splitting (>1024 chars) with threaded continuation replies
- WebP to JPEG automatic conversion for Telegram thumbnail compatibility
- State tracking via state.json with automatic GitHub Actions persistence
"""

import argparse
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Union

import instaloader
import requests

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("IgToTgBot")


def clean_username(raw: str) -> str:
    if not raw:
        return "atrbezan"
    cleaned = re.sub(r"https?://(www\.)?instagram\.com/", "", raw)
    cleaned = cleaned.strip("/@ \t\n\r")
    return cleaned or "atrbezan"


# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================
@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    instagram_username: str
    apify_token: Optional[str] = None
    instagram_session_id: Optional[str] = None
    proxy_url: Optional[str] = None
    check_interval_seconds: int = 21600
    max_posts_per_check: int = 5
    include_original_link: bool = True
    state_file_path: str = "state.json"
    download_dir: str = "downloads"

    @classmethod
    def load(cls) -> "Config":
        bot_token = (
            os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            or os.getenv("BOT_TOKEN", "").strip()
            or os.getenv("TELEGRAM_TOKEN", "").strip()
        )
        chat_id = (
            os.getenv("TELEGRAM_CHAT_ID", "").strip()
            or os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
            or os.getenv("CHANNEL_ID", "").strip()
            or "@Atrbezan"
        )
        ig_username = (
            os.getenv("INSTAGRAM_USERNAME", "").strip()
            or os.getenv("IG_USERNAME", "").strip()
            or "atrbezan"
        )
        apify_token = (
            os.getenv("APIFY_TOKEN", "").strip()
            or os.getenv("APIFY_API_TOKEN", "").strip()
            or None
        )
        session_id = (
            os.getenv("INSTAGRAM_SESSION_ID", "").strip()
            or os.getenv("IG_SESSION_ID", "").strip()
            or os.getenv("SESSION_ID", "").strip()
            or None
        )
        proxy_url = (
            os.getenv("PROXY_URL", "").strip()
            or os.getenv("HTTPS_PROXY", "").strip()
            or os.getenv("HTTP_PROXY", "").strip()
            or None
        )

        if not bot_token:
            logger.error(
                "❌ خطای مهم: متغیر TELEGRAM_BOT_TOKEN یافت نشد! "
                "لطفاً در ریپازیتوری گیت‌هاب به Settings > Secrets and variables > Actions بروید "
                "و سکرت TELEGRAM_BOT_TOKEN را با توکن ربات تلگرام تعریف کنید."
            )
            sys.exit(1)

        if not chat_id.startswith("@") and not chat_id.startswith("-"):
            chat_id = f"@{chat_id}"

        ig_username = clean_username(ig_username)

        logger.info(f"✅ تنظیمات بارگذاری شد: پیج={ig_username} | کانال={chat_id}")
        if apify_token:
            logger.info("🚀 استفاده از سرویس Apify برای دور زدن ۱۰۰٪ بلاک دیتاسنتر فعال است.")
        elif session_id:
            logger.info("🔑 نشست کوکی اینستاگرام (INSTAGRAM_SESSION_ID) فعال است.")
        else:
            logger.info("ℹ️ در حال کار به صورت عمومی (بدون سکرت لاگین یا Apify).")

        return cls(
            telegram_bot_token=bot_token,
            telegram_chat_id=chat_id,
            instagram_username=ig_username,
            apify_token=apify_token,
            instagram_session_id=session_id,
            proxy_url=proxy_url,
            check_interval_seconds=int(os.getenv("CHECK_INTERVAL_SECONDS", "21600")),
            max_posts_per_check=int(os.getenv("MAX_POSTS_PER_CHECK", "5")),
            include_original_link=os.getenv("INCLUDE_ORIGINAL_LINK", "true").lower() in ("true", "1", "yes"),
            state_file_path=os.getenv("STATE_FILE_PATH", "state.json"),
            download_dir=os.getenv("DOWNLOAD_DIR", "downloads"),
        )


# ==============================================================================
# 2. STATE MANAGER
# ==============================================================================
class StateManager:
    def __init__(self, state_file_path: str):
        self.file_path = Path(state_file_path)

    def load_posted_ids(self) -> Set[str]:
        if not self.file_path.exists():
            return set()
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("posted_shortcodes", []))
        except Exception as e:
            logger.warning(f"خطا در خواندن فایل وضعیت {self.file_path}: {e}")
            return set()

    def mark_posted(self, shortcode: str):
        posted = self.load_posted_ids()
        posted.add(shortcode)
        posted_list = list(posted)[-500:]
        data = {
            "last_check_utc": datetime.now(timezone.utc).isoformat(),
            "posted_shortcodes": posted_list,
        }
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"✅ پست {shortcode} در تاریخچه state.json ثبت شد.")


# ==============================================================================
# 3. TELEGRAM POSTER
# ==============================================================================
class TelegramPoster:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.session = requests.Session()

    def verify_bot(self) -> dict:
        url = f"{self.api_url}/getMe"
        try:
            res = self.session.get(url, timeout=15).json()
            if not res.get("ok"):
                error_desc = res.get("description", "Unknown error")
                logger.error(f"❌ توکن ربات تلگرام نامعتبر است: {error_desc}")
                logger.error("لطفاً توکن دریافتی از @BotFather را بررسی و در سکرت TELEGRAM_BOT_TOKEN اصلاح کنید.")
                sys.exit(1)
            bot_user = res["result"]["username"]
            logger.info(f"🤖 اتصال به ربات تلگرام تایید شد: @{bot_user}")
            return res["result"]
        except Exception as e:
            logger.error(f"❌ خطا در اتصال به سرور تلگرام: {e}")
            sys.exit(1)

    def verify_chat(self) -> dict:
        url = f"{self.api_url}/getChat"
        try:
            res = self.session.post(url, data={"chat_id": self.chat_id}, timeout=15).json()
            if not res.get("ok"):
                error_desc = res.get("description", "Unknown error")
                logger.error(f"❌ کانال {self.chat_id} در دسترس ربات نیست: {error_desc}")
                logger.error(
                    f"⚠️ راهنما: ربات تلگرام باید در کانال {self.chat_id} عضو شده و به عنوان Administrator (با مجوز ارسال پیام) اضافه شود."
                )
                sys.exit(1)
            title = res["result"].get("title", self.chat_id)
            logger.info(f"📢 دسترسی به کانال تلگرام تایید شد: {title} ({self.chat_id})")
            return res["result"]
        except Exception as e:
            logger.error(f"❌ خطا در بررسی کانال تلگرام: {e}")
            sys.exit(1)

    def _request(self, method: str, data: Optional[dict] = None, files: Optional[dict] = None) -> dict:
        url = f"{self.api_url}/{method}"
        for attempt in range(1, 4):
            try:
                res = self.session.post(url, data=data, files=files, timeout=90)
                res_data = res.json()
                if not res_data.get("ok"):
                    error_desc = res_data.get("description", "Unknown error")
                    error_code = res_data.get("error_code")
                    logger.error(f"Telegram API Error [{error_code}]: {error_desc}")

                    if error_code == 401:
                        logger.error("❌ توکن ربات تلگرام نامعتبر است! توکن دریافتی از @BotFather را چک کنید.")
                    elif error_code == 400 and ("chat not found" in error_desc.lower() or "not enough rights" in error_desc.lower()):
                        logger.error(f"❌ کانال {self.chat_id} پیدا نشد یا ربات ادمین نیست! مطمئن شوید ربات در کانال ادمین با مجوز ارسال پیام است.")

                    if error_code == 429:
                        wait_sec = res_data.get("parameters", {}).get("retry_after", 10)
                        logger.warning(f"محدودیت نرخ تلگرام (Rate-limit). انتظار {wait_sec} ثانیه...")
                        time.sleep(wait_sec)
                        continue

                    raise RuntimeError(f"Telegram API error [{error_code}]: {error_desc}")
                return res_data
            except requests.RequestException as e:
                logger.warning(f"خطای درخواست تلگرام (تلاش {attempt}/3): {e}")
                time.sleep(2 * attempt)
        raise RuntimeError(f"ارسال به تلگرام در متد {method} پس از ۳ تلاش با شکست مواجه شد.")

    def split_caption(self, caption: str) -> tuple[str, Optional[str]]:
        if not caption:
            return "", None
        caption = caption.strip()
        if len(caption) <= 1024:
            return caption, None

        cut_idx = 1000
        last_nl = caption[:cut_idx].rfind("\n")
        if last_nl > 700:
            cut_idx = last_nl
        short_cap = caption[:cut_idx].strip() + "\n\n...(ادامه کپشن در پیام بعد ⬇️)"
        extra = caption[cut_idx:].strip()
        return short_cap, extra

    def send_message(self, text: str, reply_to_message_id: Optional[int] = None) -> dict:
        data = {"chat_id": self.chat_id, "text": text[:4000]}
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id
        return self._request("sendMessage", data=data)

    def send_photo(self, photo_path: str, caption: str = "") -> dict:
        short_caption, extra_text = self.split_caption(caption)
        data = {"chat_id": self.chat_id, "caption": short_caption}
        with open(photo_path, "rb") as f:
            files = {"photo": (os.path.basename(photo_path), f, "image/jpeg")}
            res = self._request("sendPhoto", data=data, files=files)
        if extra_text:
            msg_id = res.get("result", {}).get("message_id")
            self.send_message(f"ادامه کپشن:\n\n{extra_text}", reply_to_message_id=msg_id)
        return res

    def send_video(self, video_path: str, caption: str = "", thumb_path: Optional[str] = None) -> dict:
        short_caption, extra_text = self.split_caption(caption)
        data = {"chat_id": self.chat_id, "caption": short_caption, "supports_streaming": True}
        files = {}
        handlers = []
        try:
            vf = open(video_path, "rb")
            handlers.append(vf)
            files["video"] = (os.path.basename(video_path), vf, "video/mp4")
            if thumb_path and os.path.exists(thumb_path):
                tf = open(thumb_path, "rb")
                handlers.append(tf)
                files["thumbnail"] = (os.path.basename(thumb_path), tf, "image/jpeg")
            res = self._request("sendVideo", data=data, files=files)
        finally:
            for h in handlers:
                h.close()
        if extra_text:
            msg_id = res.get("result", {}).get("message_id")
            self.send_message(f"ادامه کپشن:\n\n{extra_text}", reply_to_message_id=msg_id)
        return res

    def send_media_group(self, media_files: List[dict], caption: str = "") -> list[dict]:
        short_caption, extra_text = self.split_caption(caption)
        results = []
        chunks = [media_files[i : i + 10] for i in range(0, len(media_files), 10)]

        for chunk_idx, chunk in enumerate(chunks):
            media_array = []
            files = {}
            handlers = []
            try:
                for idx, item in enumerate(chunk):
                    file_key = f"file_{idx}"
                    file_path = item["path"]
                    fh = open(file_path, "rb")
                    handlers.append(fh)
                    mtype = item["type"]
                    mime = "video/mp4" if mtype == "video" else "image/jpeg"
                    files[file_key] = (os.path.basename(file_path), fh, mime)

                    obj = {"type": mtype, "media": f"attach://{file_key}"}
                    if chunk_idx == 0 and idx == 0 and short_caption:
                        obj["caption"] = short_caption
                    if mtype == "video":
                        obj["supports_streaming"] = True
                        if item.get("thumb") and os.path.exists(item["thumb"]):
                            tkey = f"thumb_{idx}"
                            tfh = open(item["thumb"], "rb")
                            handlers.append(tfh)
                            files[tkey] = (os.path.basename(item["thumb"]), tfh, "image/jpeg")
                            obj["thumbnail"] = f"attach://{tkey}"

                    media_array.append(obj)

                data = {"chat_id": self.chat_id, "media": json.dumps(media_array)}
                res = self._request("sendMediaGroup", data=data, files=files)
                results.append(res)
            finally:
                for h in handlers:
                    h.close()
            if chunk_idx < len(chunks) - 1:
                time.sleep(1.5)

        if extra_text and results:
            msg_id = results[0].get("result", [{}])[0].get("message_id")
            self.send_message(f"ادامه کپشن:\n\n{extra_text}", reply_to_message_id=msg_id)

        return results


# ==============================================================================
# 4. INSTAGRAM DOWNLOADER & SCRAPER
# ==============================================================================
@dataclass
class InstagramPostItem:
    shortcode: str
    url: str
    is_video: bool
    typename: str
    caption: str
    date_utc: datetime
    media_files: List[dict] = field(default_factory=list)


class InstagramDownloader:
    def __init__(
        self,
        target_username: str,
        download_dir: str = "downloads",
        apify_token: Optional[str] = None,
        session_id: Optional[str] = None,
        proxy_url: Optional[str] = None,
    ):
        self.target_username = clean_username(target_username)
        self.download_dir = Path(download_dir).resolve()
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.apify_token = apify_token
        self.session_id = session_id
        self.proxy_url = proxy_url

        if proxy_url:
            os.environ["HTTP_PROXY"] = proxy_url
            os.environ["HTTPS_PROXY"] = proxy_url

        self.loader = instaloader.Instaloader(
            dirname_pattern=str(self.download_dir / "{target}"),
            filename_pattern="{shortcode}_{date_utc:%Y%m%d_%H%M%S}",
            download_pictures=True,
            download_videos=True,
            download_video_thumbnails=True,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            max_connection_attempts=1,
        )

        if self.session_id:
            try:
                self.loader.context.update_cookies({"sessionid": self.session_id})
                if "%3A" in self.session_id:
                    uid = self.session_id.split("%3A")[0]
                    self.loader.context.update_cookies({"ds_user_id": uid})
                elif ":" in self.session_id:
                    uid = self.session_id.split(":")[0]
                    self.loader.context.update_cookies({"ds_user_id": uid})
                logger.info("🔑 کوکی احراز هویت اینستاگرام با موفقیت روی لودر بارگذاری شد.")
            except Exception as e:
                logger.warning(f"خطا در اعمال کوکی sessionid: {e}")

    def _fetch_from_apify(self, limit: int = 5) -> List[dict]:
        """Fetch posts via Apify Instagram Scraper (Residential proxies)."""
        if not self.apify_token:
            return []
        logger.info(f"🌐 در حال دریافت پست‌ها از طریق Apify برای پیج {self.target_username}...")
        url = "https://api.apify.com/v2/acts/apify~instagram-scraper/run-sync-get-dataset-items"
        params = {"token": self.apify_token, "timeout": 60}
        payload = {
            "directUrls": [f"https://www.instagram.com/{self.target_username}/"],
            "resultsType": "posts",
            "resultsLimit": limit,
        }
        try:
            res = requests.post(url, params=params, json=payload, timeout=90)
            if res.status_code == 200 or res.status_code == 201:
                items = res.json()
                logger.info(f"✅ تعداد {len(items)} آیتم از Apify دریافت شد.")
                posts = []
                for item in items:
                    sc = item.get("shortCode") or item.get("shortcode")
                    if not sc and item.get("url"):
                        m = re.search(r'/(?:p|reel)/([A-Za-z0-9_-]+)', item["url"])
                        if m:
                            sc = m.group(1)
                    if sc:
                        posts.append({
                            "shortcode": sc,
                            "url": item.get("url") or f"https://www.instagram.com/reel/{sc}/",
                            "is_video": item.get("isVideo", True),
                            "typename": "GraphVideo" if item.get("isVideo", True) else "GraphImage",
                            "caption": item.get("caption") or "",
                            "date_utc": datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")) if item.get("timestamp") else datetime.now(timezone.utc),
                            "video_url": item.get("videoUrl"),
                            "display_url": item.get("displayUrl"),
                        })
                return posts
            else:
                logger.warning(f"خطای پاسخ Apify: {res.status_code} - {res.text[:200]}")
        except Exception as e:
            logger.warning(f"خطا در اتصال به Apify: {e}")
        return []

    def _fetch_shortcodes(self) -> List[str]:
        """Fetch shortcodes via public embed interface with multiple fallback user-agents."""
        url = f"https://www.instagram.com/{self.target_username}/embed/"
        user_agents = [
            None,
            "curl/7.88.1",
            "TelegramBot (like TwitterBot)",
            "facebookexternalhit/1.1 (+https://www.facebook.com/externalhit_uatext.php)",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
        ]

        cookies = {}
        if self.session_id:
            cookies["sessionid"] = self.session_id

        for ua in user_agents:
            headers = {"User-Agent": ua} if ua else {}
            try:
                res = requests.get(url, headers=headers, cookies=cookies, timeout=12)
                if res.status_code == 200:
                    pattern = r'shortcode_media\\":\{.*?\\"shortcode\\":\\"([A-Za-z0-9_-]+)\\"'
                    matches = re.findall(pattern, res.text)
                    if not matches:
                        matches = re.findall(r'\\"shortcode\\":\\"([A-Za-z0-9_-]{10,13})\\"', res.text)
                    seen = set()
                    shortcodes = [x for x in matches if not (x in seen or seen.add(x))]
                    if shortcodes:
                        logger.info(f"✅ شورت‌کدهای استخراج شده از اینستاگرام: {shortcodes}")
                        return shortcodes
            except Exception:
                pass

        return []

    def _get_ytdlp_metadata(self, shortcode: str) -> Optional[dict]:
        """Fetch post/reel metadata using yt-dlp."""
        for u_type in ["reel", "p"]:
            url = f"https://www.instagram.com/{u_type}/{shortcode}/"
            cmd = ["yt-dlp", "--dump-json", "--no-warnings", url]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
                if res.returncode == 0 and res.stdout.strip():
                    return json.loads(res.stdout.strip())
            except Exception:
                pass
        return None

    def get_latest_posts(self, limit: int = 5) -> List[Union[instaloader.Post, dict]]:
        # 1. Try Apify if configured
        if self.apify_token:
            apify_posts = self._fetch_from_apify(limit=limit)
            if apify_posts:
                return apify_posts

        # 2. Try embed interface shortcodes
        shortcodes = self._fetch_shortcodes()
        posts = []

        if shortcodes:
            for sc in shortcodes[:limit]:
                try:
                    p = instaloader.Post.from_shortcode(self.loader.context, sc)
                    posts.append(p)
                    continue
                except Exception:
                    pass

                meta = self._get_ytdlp_metadata(sc)
                if meta:
                    posts.append(
                        {
                            "shortcode": sc,
                            "url": f"https://www.instagram.com/reel/{sc}/",
                            "is_video": True,
                            "typename": "GraphVideo",
                            "caption": meta.get("description") or "",
                            "date_utc": datetime.fromtimestamp(meta.get("timestamp", time.time()), tz=timezone.utc),
                        }
                    )

            if posts:
                return posts

        # 3. Fallback to Profile.get_posts
        try:
            profile = instaloader.Profile.from_username(self.loader.context, self.target_username)
            for p in profile.get_posts():
                posts.append(p)
                if len(posts) >= limit:
                    break
            return posts
        except Exception as e:
            logger.warning(f"عدم دسترسی به Profile.get_posts: {e}")

        return posts

    def _ensure_jpeg_thumbnail(self, thumb_path: Optional[str]) -> Optional[str]:
        if not thumb_path or not os.path.exists(thumb_path):
            return None
        ext = os.path.splitext(thumb_path)[1].lower()
        if ext in [".jpg", ".jpeg"]:
            return thumb_path
        if not HAS_PIL:
            return thumb_path
        try:
            target_jpg = os.path.splitext(thumb_path)[0] + ".jpg"
            with Image.open(thumb_path) as img:
                img.convert("RGB").save(target_jpg, "JPEG", quality=90)
            return target_jpg
        except Exception:
            return thumb_path

    def download_post(self, post_item: Union[instaloader.Post, dict]) -> InstagramPostItem:
        if isinstance(post_item, instaloader.Post):
            sc = post_item.shortcode
            is_video = post_item.is_video
            typename = post_item.typename
            caption = post_item.caption or ""
            date_utc = post_item.date_utc
            url = f"https://www.instagram.com/p/{sc}/"
        else:
            sc = post_item["shortcode"]
            is_video = post_item.get("is_video", True)
            typename = post_item.get("typename", "GraphVideo")
            caption = post_item.get("caption", "")
            date_utc = post_item.get("date_utc", datetime.now(timezone.utc))
            url = post_item.get("url", f"https://www.instagram.com/reel/{sc}/")

        post_dir = self.download_dir / sc
        if post_dir.exists():
            shutil.rmtree(post_dir, ignore_errors=True)
        post_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"📥 شروع دانلود رسانه {sc} ({typename})...")

        if isinstance(post_item, instaloader.Post):
            try:
                self.loader.download_post(post_item, target=sc)
            except Exception:
                pass

        videos = sorted(glob.glob(str(post_dir / "*.mp4")))
        images = sorted(
            glob.glob(str(post_dir / "*.jpg"))
            + glob.glob(str(post_dir / "*.png"))
            + glob.glob(str(post_dir / "*.webp"))
        )

        # Fallback to yt-dlp if video is missing
        if is_video and not videos:
            logger.info(f"دانلود ویدیو با yt-dlp برای {sc}...")
            out_tmpl = str(post_dir / f"{sc}.%(ext)s")
            cmd = [
                "yt-dlp",
                "--no-warnings",
                "--write-thumbnail",
                "-o",
                out_tmpl,
                f"https://www.instagram.com/reel/{sc}/",
            ]
            subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            videos = sorted(glob.glob(str(post_dir / "*.mp4")))
            images = sorted(
                glob.glob(str(post_dir / "*.jpg"))
                + glob.glob(str(post_dir / "*.png"))
                + glob.glob(str(post_dir / "*.webp"))
            )

        media_items = []
        if is_video and typename != "GraphSidecar":
            if videos:
                raw_thumb = images[0] if images else None
                thumb = self._ensure_jpeg_thumbnail(raw_thumb)
                media_items.append({"type": "video", "path": videos[0], "thumb": thumb})
        elif typename == "GraphSidecar":
            video_bases = {os.path.splitext(v)[0] for v in videos}
            for v in videos:
                base = os.path.splitext(v)[0]
                t = (base + ".jpg") if os.path.exists(base + ".jpg") else None
                media_items.append({"type": "video", "path": v, "thumb": self._ensure_jpeg_thumbnail(t)})
            for img in images:
                base = os.path.splitext(img)[0]
                if base not in video_bases:
                    media_items.append({"type": "photo", "path": img, "thumb": None})
            media_items.sort(key=lambda x: x["path"])
        else:
            if images:
                media_items.append({"type": "photo", "path": images[0], "thumb": None})

        return InstagramPostItem(
            shortcode=sc,
            url=url,
            is_video=is_video,
            typename=typename,
            caption=caption,
            date_utc=date_utc,
            media_files=media_items,
        )

    def cleanup_post_dir(self, shortcode: str):
        post_dir = self.download_dir / shortcode
        if post_dir.exists():
            shutil.rmtree(post_dir, ignore_errors=True)


# ==============================================================================
# 5. AGENT COORDINATOR
# ==============================================================================
class InstagramTelegramAgent:
    def __init__(self, config: Config):
        self.config = config
        self.state = StateManager(config.state_file_path)
        self.tg = TelegramPoster(config.telegram_bot_token, config.telegram_chat_id)
        # Perform self-checks
        self.tg.verify_bot()
        self.tg.verify_chat()

        self.ig = InstagramDownloader(
            config.instagram_username,
            config.download_dir,
            apify_token=config.apify_token,
            session_id=config.instagram_session_id,
            proxy_url=config.proxy_url,
        )

    def format_caption(self, item: InstagramPostItem) -> str:
        caption = item.caption or ""
        if self.config.include_original_link:
            link = f"\n\n🔗 [مشاهده در اینستاگرام]({item.url})"
            caption = caption.strip() + link
        return caption.strip()

    def post_to_telegram(self, item: InstagramPostItem):
        caption = self.format_caption(item)
        files = item.media_files
        if not files:
            logger.warning(f"رسانه‌ای برای دانلود یافت نشد. ارسال متن و لینک {item.shortcode}...")
            self.tg.send_message(f"{caption}\n\n{item.url}")
            return

        if len(files) == 1:
            media = files[0]
            if media["type"] == "video":
                logger.info(f"📤 ارسال ویدیو/ریلز {item.shortcode} به تلگرام...")
                self.tg.send_video(media["path"], caption=caption, thumb_path=media.get("thumb"))
            else:
                logger.info(f"📤 ارسال تصویر {item.shortcode} به تلگرام...")
                self.tg.send_photo(media["path"], caption=caption)
        else:
            logger.info(f"📤 ارسال آلبوم چند رسانه‌ای ({len(files)} آیتم) {item.shortcode} به تلگرام...")
            self.tg.send_media_group(files, caption=caption)

    def check_and_sync(self):
        logger.info(f"🔍 بررسی آخرین پست‌های پیج @{self.config.instagram_username}...")
        already_posted = self.state.load_posted_ids()
        recent_posts = self.ig.get_latest_posts(limit=self.config.max_posts_per_check)

        if not recent_posts:
            msg = (
                "❌ خطای عدم دسترسی: سرورهای گیت‌هاب (Microsoft Azure) توسط اینستاگرام مسدود شده‌اند (کد ۴۲۹ یا ریدایرکت به لاگین).\n"
                "به همین دلیل هیچ پستی از اینستاگرام دریافت نشد.\n\n"
                "💡 راه‌حل قطعی و ۱۰۰٪ رایگان:\n"
                "سکرت APIFY_TOKEN یا INSTAGRAM_SESSION_ID را در تنظیمات گیت‌هاب اضافه کنید تا درخواست‌ها مسدود نشوند."
            )
            logger.error(msg)
            sys.exit(1)

        new_posts = []
        for p in recent_posts:
            sc = p.shortcode if isinstance(p, instaloader.Post) else p["shortcode"]
            if sc not in already_posted:
                new_posts.append(p)

        if not new_posts:
            logger.info("✅ همه پست‌های بررسی شده قبلاً در کانال تلگرام ارسال شده‌اند.")
            return

        logger.info(f"🎉 تعداد {len(new_posts)} پست جدید پیدا شد! شروع پردازش و ارسال...")
        new_posts.sort(
            key=lambda p: p.date_utc if isinstance(p, instaloader.Post) else p.get("date_utc", datetime.now(timezone.utc))
        )

        errors = []
        for post in new_posts:
            sc = post.shortcode if isinstance(post, instaloader.Post) else post["shortcode"]
            try:
                item = self.ig.download_post(post)
                self.post_to_telegram(item)
                self.state.mark_posted(sc)
                self.ig.cleanup_post_dir(sc)
                logger.info(f"✨ پست {sc} با موفقیت به تلگرام ارسال و ثبت شد.")
                time.sleep(3)
            except Exception as e:
                err_msg = f"پست {sc}: {e}"
                logger.error(f"❌ خطا در پردازش یا ارسال {err_msg}")
                errors.append(err_msg)

        if errors and len(errors) == len(new_posts):
            logger.error("❌ هیچ‌کدام از پست‌های جدید با موفقیت به تلگرام ارسال نشدند!")
            sys.exit(1)

    def run_daemon(self):
        logger.info(f"ربات در حال کار است. بررسی هر {self.config.check_interval_seconds} ثانیه...")
        while True:
            try:
                self.check_and_sync()
            except Exception as e:
                logger.error(f"خطا در اجرای دوره‌ای: {e}")
            time.sleep(self.config.check_interval_seconds)


# ==============================================================================
# 6. ENTRY POINT
# ==============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--single-run", action="store_true", help="Run once and exit (for GitHub Actions)")
    args = parser.parse_args()

    config = Config.load()
    agent = InstagramTelegramAgent(config)

    if args.single_run:
        agent.check_and_sync()
    else:
        agent.run_daemon()


if __name__ == "__main__":
    main()
