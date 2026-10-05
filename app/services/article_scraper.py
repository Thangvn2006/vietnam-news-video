import os
import re
import ipaddress
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, List, Union

import requests
from bs4 import BeautifulSoup
from loguru import logger
from PIL import Image

from app.utils import utils


DEFAULT_SCRAPER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# Common suffixes appended to news titles by media outlets
COMMON_TITLE_SUFFIXES = [
    r"\s*[-|–—]\s*VnExpress.*$",
    r"\s*[-|–—]\s*Báo Dân trí.*$",
    r"\s*[-|–—]\s*Dân trí.*$",
    r"\s*[-|–—]\s*Tuổi Trẻ Online.*$",
    r"\s*[-|–—]\s*Báo Tuổi Trẻ.*$",
    r"\s*[-|–—]\s*Báo Thanh Niên.*$",
    r"\s*[-|–—]\s*Thanh Niên.*$",
    r"\s*[-|–—]\s*VietNamNet.*$",
    r"\s*[-|–—]\s*Báo điện tử VTV.*$",
    r"\s*[-|–—]\s*VTV\.vn.*$",
    r"\s*[-|–—]\s*Znews.*$",
    r"\s*[-|–—]\s*Zing.*$",
    r"\s*[-|–—]\s*Báo Lao Động.*$",
    r"\s*[-|–—]\s*Báo Tiền Phong.*$",
    r"\s*[-|–—]\s*BBC News Tiếng Việt.*$",
    r"\s*[-|–—]\s*VOV.*$",
    r"\s*[-|–—]\s*Kenh14.*$",
    r"\s*[-|–—]\s*CafeF.*$",
]

# Elements to remove when extracting article body
UNWANTED_ELEMENTS = [
    "script",
    "style",
    "noscript",
    "iframe",
    "header",
    "footer",
    "nav",
    "aside",
    "form",
    "svg",
    ".box-comment",
    ".comment",
    ".social-share",
    ".share-post",
    ".relate-container",
    ".related-news",
    ".banner",
    ".advertisement",
    ".ads",
    ".ad-banner",
    ".box_tinkhac",
    ".tag-container",
    ".author-info",
]


@dataclass
class ScrapedImage:
    url: str
    alt: str = ""
    caption: str = ""


@dataclass
class ScrapedArticle:
    url: str
    title: str = ""
    summary: str = ""
    content: str = ""
    images: List[ScrapedImage] = field(default_factory=list)
    authors: List[str] = field(default_factory=list)
    publish_date: str = ""
    domain: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "summary": self.summary,
            "content": self.content,
            "images": [
                {"url": img.url, "alt": img.alt, "caption": img.caption}
                for img in self.images
            ],
            "authors": self.authors,
            "publish_date": self.publish_date,
            "domain": self.domain,
            "source_name": get_news_source_name(self.url or self.domain),
        }


def get_news_source_name(url_or_domain: Any) -> str:
    """Format URL, domain, or ScrapedArticle into a clean brand name (e.g. VnExpress, Tuổi Trẻ, Dân Trí)."""
    if not url_or_domain:
        return ""
    if isinstance(url_or_domain, ScrapedArticle):
        raw = url_or_domain.domain or url_or_domain.url or ""
    elif isinstance(url_or_domain, str):
        raw = url_or_domain
    else:
        raw = getattr(url_or_domain, "domain", "") or getattr(url_or_domain, "url", "") or str(url_or_domain)

    if not raw:
        return ""
    if "://" in raw:
        domain = urllib.parse.urlparse(raw).netloc
    else:
        domain = raw
    domain = domain.lower().replace("www.", "").strip()

    domain_map = {
        "vnexpress.net": "VnExpress",
        "tuoitre.vn": "Tuổi Trẻ",
        "dantri.com.vn": "Dân Trí",
        "thanhnien.vn": "Thanh Niên",
        "vietnamnet.vn": "VietNamNet",
        "laodong.vn": "Báo Lao Động",
        "tienphong.vn": "Báo Tiền Phong",
        "zingnews.vn": "Zing News",
        "znews.vn": "ZNews",
        "baochinhphu.vn": "Báo Chính Phủ",
        "vtv.vn": "VTV News",
        "kenh14.vn": "Kênh 14",
        "cafef.vn": "CafeF",
        "cafebiz.vn": "CafeBiz",
        "soha.vn": "Soha",
        "24h.com.vn": "24h",
        "nld.com.vn": "Người Lao Động",
        "plo.vn": "Pháp Luật TP.HCM",
        "sggp.org.vn": "Sài Gòn Giải Phóng",
        "cand.com.vn": "Công An Nhân Dân",
        "qdnd.vn": "Quân Đội Nhân Dân",
    }
    for d, brand in domain_map.items():
        if d in domain:
            return brand

    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[0].capitalize()
    return domain.capitalize() if domain else ""



def is_safe_url(url: str) -> bool:
    """Validate that the URL is a safe HTTP or HTTPS URL, preventing SSRF."""
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme.lower() not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        # Disallow localhost / loopback / private IP ranges
        if hostname.lower() in ("localhost", "127.0.0.1", "::1"):
            return False
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return False
        except ValueError:
            # It's a standard domain name, safe to proceed
            pass
        return True
    except Exception:
        return False


def _clean_title(title: str) -> str:
    cleaned = title.strip()
    for suffix_pattern in COMMON_TITLE_SUFFIXES:
        cleaned = re.sub(suffix_pattern, "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def scrape_article(
    url: str,
    timeout: int = 15,
    user_agent: str | None = None,
) -> ScrapedArticle:
    """
    Scrape article headline, summary, body content, and media images from a news URL.
    """
    url = url.strip()
    if not is_safe_url(url):
        raise ValueError(f"Invalid or disallowed URL: '{url}'")

    headers = {
        "User-Agent": user_agent or DEFAULT_SCRAPER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "vi,en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        )
        response.raise_for_status()
        # Handle correct encoding (especially Vietnamese utf-8)
        if response.encoding is None or response.encoding.lower() == "iso-8859-1":
            response.encoding = response.apparent_encoding or "utf-8"
        html = response.text
    except Exception as exc:
        logger.error(f"Failed to fetch article from {url}: {exc}")
        raise RuntimeError(f"Could not download article content: {exc}") from exc

    return parse_article_html(html=html, url=url)


def parse_article_html(html: str, url: str) -> ScrapedArticle:
    """Parse article HTML content into a structured ScrapedArticle."""
    soup = BeautifulSoup(html, "html.parser")
    domain = urllib.parse.urlparse(url).netloc

    # 1. Title Extraction
    title = ""
    # Try OpenGraph or Twitter title first
    og_title = soup.find("meta", property="og:title") or soup.find(
        "meta", attrs={"name": "og:title"}
    )
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()

    if not title:
        tw_title = soup.find("meta", attrs={"name": "twitter:title"})
        if tw_title and tw_title.get("content"):
            title = tw_title["content"].strip()

    if not title:
        # Try common main heading selectors
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

    if not title and soup.title:
        title = soup.title.get_text(strip=True)

    title = _clean_title(title)

    # 2. Summary / Description Extraction
    summary = ""
    og_desc = soup.find("meta", property="og:description") or soup.find(
        "meta", attrs={"name": "description"}
    )
    if og_desc and og_desc.get("content"):
        summary = og_desc["content"].strip()

    if not summary:
        sapo_el = soup.select_one("p.description, p.sapo, p.lead, h2.sapo, div.sapo")
        if sapo_el:
            summary = sapo_el.get_text(strip=True)

    # 3. Author & Published Date
    authors = []
    author_meta = soup.find("meta", attrs={"name": "author"})
    if author_meta and author_meta.get("content"):
        authors.append(author_meta["content"].strip())

    publish_date = ""
    date_meta = (
        soup.find("meta", property="article:published_time")
        or soup.find("meta", attrs={"name": "pubdate"})
        or soup.find("meta", attrs={"name": "publishdate"})
    )
    if date_meta and date_meta.get("content"):
        publish_date = date_meta["content"].strip()

    # 4. Content Container Detection
    content_container = None
    container_selectors = [
        "article",
        "div.fck_detail",             # VnExpress
        "div.singular-content",          # Dan Tri
        "div.detail-content",            # Tuoi Tre, Thanh Nien
        "div.maincontent",               # VietnamNet
        "div.content-detail",
        "div[itemprop='articleBody']",
        "div.article__body",
        "div.article-content",
        "div.entry-content",
        "div.post-content",
        "div.content",
    ]
    for selector in container_selectors:
        found = soup.select_one(selector)
        if found:
            content_container = found
            break

    search_root = content_container if content_container else (soup.body or soup)

    # 5. Extract Images before removing elements
    scraped_images: List[ScrapedImage] = []
    seen_image_urls = set()

    # Check og:image first
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        raw_og = og_image["content"].strip()
        full_og = urllib.parse.urljoin(url, raw_og)
        if _is_valid_image_url(full_og):
            scraped_images.append(ScrapedImage(url=full_og, alt=title, caption="Cover"))
            seen_image_urls.add(full_og)

    # Find <img> tags in search_root
    for img in search_root.find_all("img"):
        img_src = (
            img.get("data-src")
            or img.get("data-original")
            or img.get("data-lazy")
            or img.get("src")
            or ""
        ).strip()

        if not img_src or img_src.startswith("data:"):
            # Check srcset if src was missing
            srcset = img.get("srcset") or img.get("data-srcset")
            if srcset:
                parts = srcset.split(",")
                if parts:
                    img_src = parts[-1].strip().split()[0]

        if not img_src:
            continue

        full_img_url = urllib.parse.urljoin(url, img_src)
        if full_img_url in seen_image_urls or not _is_valid_image_url(full_img_url):
            continue

        alt_text = (img.get("alt") or "").strip()

        # Find caption from surrounding figure or adjacent paragraph
        caption_text = ""
        parent = img.parent
        if parent:
            figcaption = parent.find("figcaption")
            if figcaption:
                caption_text = figcaption.get_text(strip=True)
            elif parent.name == "figure":
                fig_desc = parent.select_one(".ImageDescription, .img-desc, .caption")
                if fig_desc:
                    caption_text = fig_desc.get_text(strip=True)

        scraped_images.append(
            ScrapedImage(
                url=full_img_url,
                alt=alt_text,
                caption=caption_text or alt_text,
            )
        )
        seen_image_urls.add(full_img_url)

    # 6. Extract Paragraph Texts
    # Remove unwanted sub-elements
    for selector in UNWANTED_ELEMENTS:
        for element in search_root.select(selector):
            element.decompose()

    paragraphs = []
    for p in search_root.find_all(["p", "div"]):
        # Only take direct-ish text paragraphs, avoid huge parent wrappers
        if p.name == "div" and p.find_all("p"):
            continue
        text = p.get_text(separator=" ", strip=True)
        # Filter out short fragments, copyright, share tags
        if len(text) >= 25 and not _is_noise_text(text):
            if text not in paragraphs and text != title and text != summary:
                paragraphs.append(text)

    full_content = "\n\n".join(paragraphs).strip()
    if not full_content and summary:
        full_content = summary

    return ScrapedArticle(
        url=url,
        title=title,
        summary=summary,
        content=full_content,
        images=scraped_images,
        authors=authors,
        publish_date=publish_date,
        domain=domain,
    )


def _is_valid_image_url(image_url: str) -> bool:
    """Filter out non-image, tiny tracking pixels, or SVG icon URLs."""
    lower = image_url.lower()
    if any(ext in lower for ext in (".svg", ".ico", "data:image")):
        return False
    if any(noise in lower for noise in ("avatar", "logo", "icon", "tracking", "pixel", "banner_ad")):
        return False
    return lower.startswith("http://") or lower.startswith("https://")


def _is_noise_text(text: str) -> bool:
    lower = text.lower()
    noise_keywords = [
        "chia sẻ bài viết",
        "bình luận",
        "theo dõi chúng tôi",
        "tin liên quan",
        "đọc thêm",
        "bài viết liên quan",
        "xem thêm:",
        "nguồn:",
        "copyright",
        "bản quyền thuộc",
    ]
    return any(noise in lower for noise in noise_keywords)


def download_article_images(
    images: List[ScrapedImage],
    output_dir: str | None = None,
    save_dir: str | None = None,
    max_images: int = 10,
    min_width: int = 200,
    min_height: int = 150,
    timeout: int = 10,
) -> List[str]:
    """
    Download article images to local materials directory and validate they are usable images.
    Returns list of local file paths.
    """
    if not images:
        return []

    target_dir = output_dir or save_dir or utils.storage_dir("local_videos", create=True)
    os.makedirs(target_dir, exist_ok=True)

    saved_paths: List[str] = []
    headers = {"User-Agent": DEFAULT_SCRAPER_USER_AGENT}

    for idx, img in enumerate(images[: max_images * 2]):
        if len(saved_paths) >= max_images:
            break

        img_url = img.url
        try:
            resp = requests.get(img_url, headers=headers, timeout=timeout, stream=True)
            if resp.status_code != 200:
                continue

            content_type = resp.headers.get("Content-Type", "").lower()
            if "image" not in content_type and not any(
                ext in img_url.lower() for ext in (".jpg", ".jpeg", ".png", ".webp")
            ):
                continue

            # Determine file extension
            ext = ".jpg"
            if "png" in content_type or ".png" in img_url.lower():
                ext = ".png"
            elif "webp" in content_type or ".webp" in img_url.lower():
                ext = ".png"  # Convert webp to png for maximum moviepy compatibility

            file_stem = f"news_material_{idx + 1}_{utils.md5(img_url)[:8]}"
            out_path = os.path.join(target_dir, f"{file_stem}{ext}")

            # Download to memory and validate with PIL
            data = resp.content
            if len(data) < 2048:  # Skip files smaller than 2KB
                continue

            import io
            with Image.open(io.BytesIO(data)) as pil_img:
                width, height = pil_img.size
                if width < min_width or height < min_height:
                    continue
                # Convert RGBA/Palette/WebP to RGB JPEG for moviepy compatibility
                if pil_img.mode in ("RGBA", "P", "LA"):
                    rgb_img = Image.new("RGB", pil_img.size, (255, 255, 255))
                    rgb_img.paste(pil_img, mask=pil_img.split()[-1] if pil_img.mode in ("RGBA", "LA") else None)
                    processed_img = rgb_img
                else:
                    processed_img = pil_img.convert("RGB")

                # Upscale image proportionally if either dimension is below 480px,
                # ensuring full compatibility with MoviePy's resolution verification.
                w, h = processed_img.size
                if w < 480 or h < 480:
                    scale = max(480 / w, 480 / h)
                    target_w = int(round(w * scale))
                    target_h = int(round(h * scale))
                    processed_img = processed_img.resize((target_w, target_h), Image.Resampling.LANCZOS)

                processed_img.save(out_path, "JPEG", quality=95)

            saved_paths.append(out_path)
            logger.info(f"Downloaded news image #{len(saved_paths)}: {out_path} ({width}x{height})")
        except Exception as exc:
            logger.warning(f"Skipping image {img_url} due to download error: {exc}")
            continue

    return saved_paths


def generate_gemini_news_prompt(
    article: ScrapedArticle,
    target_duration: Union[int, str] = 60,
    language: str = "vi",
) -> str:
    """
    Generate an optimal prompt for Google Gemini to extract video components
    (Subject, Narration Script, Footage Search Keywords, Image Prompts) from the article.
    """
    if isinstance(target_duration, str):
        if not target_duration.isdigit():
            # Caller passed (article, language) positionally
            language = target_duration
            target_duration = 60
        else:
            target_duration = int(target_duration)
    target_duration = max(15, int(target_duration))

    lang_instruction = f"Toàn bộ tiêu đề và kịch bản thuyết minh phải được viết bằng {language}." if language and language != "auto" else "Giữ nguyên ngôn ngữ gốc của bài báo."
    return f"""Bạn là một chuyên gia sáng tạo nội dung video ngắn (Shorts / Reels / TikTok) và biên tập viên tin tức truyền hình chuyên nghiệp.

Dưới đây là thông tin bài báo vừa cào được:
---
- TIÊU ĐỀ BÁO: {article.title}
- NGUỒN / TÊN MIỀN: {article.domain}
- SAPO / TÓM TẮT: {article.summary or "Không có"}
- NỘI DUNG CHI TIẾT:
{article.content[:3500]}
---

NHIỆM VỤ CỦA BẠN:
Hãy phân tích nội dung trên và tạo kịch bản hoàn chỉnh để sản xuất video ngắn thời lượng khoảng {target_duration} giây. {lang_instruction}

HÃY TRẢ VỀ THEO ĐỊNH DẠNG SAU:

1. VIDEO SUBJECT (Tiêu đề video - ngắn gọn, giật gân, cuốn hút người xem trong 3 giây đầu):
[Điền tiêu đề tại đây]

2. VIDEO SCRIPT (Lời bình thuyết minh / Voiceover):
- Viết kịch bản khoảng {target_duration} giây (khoảng {int(target_duration * 3.5)} đến {int(target_duration * 4.2)} từ).
- Văn phong tin tức nhanh, sắc bén, hấp dẫn, dễ hiểu.
- Câu cú gãy gọn, tự nhiên, ngắt nghỉ hợp lý cho giọng đọc AI (TTS).
- Bố cục 3 phần: Hook mở đầu gây tò mò -> Diễn biến cốt lõi -> Kết luận/Kêu gọi bình luận.
[Điền toàn bộ lời bình tại đây, KHÔNG kèm chú thích đạo diễn/âm thanh]

3. VIDEO SEARCH TERMS (5 - 8 từ khóa tiếng Anh để tìm video/hình ảnh minh họa phù hợp từng đoạn):
[Ví dụ: breaking news, vietnam economy, busy street, technology meeting, modern factory]

4. IMAGE PROMPTS (3 - 5 câu lệnh tiếng Anh chi tiết để tạo ảnh minh họa bằng AI nếu cần):
[Prompt 1: Cinematic photorealistic shot of...]
[Prompt 2: ...]
"""


def build_article_reading_script(
    article: ScrapedArticle,
    max_words: int = 250,
    mode: str = "concise",
) -> str:
    """
    Format scraped article into a natural, spoken news narration for TTS.

    In 'concise' mode (default), limits to ~max_words for short video formats (Shorts/Reels/TikTok).
    In 'full' mode, includes the full article body.
    Cleans up news artifacts (photo credits, timestamps, editorial signatures, etc.).
    """
    pieces = []

    # 1. Headline / Hook
    clean_title = (article.title or "").strip()
    if clean_title:
        if not clean_title.endswith((".", "!", "?")):
            clean_title += "."
        pieces.append(clean_title)

    # 2. Sapo / Summary
    clean_summary = (article.summary or "").strip()
    if clean_summary and clean_summary != clean_title:
        if not clean_summary.endswith((".", "!", "?")):
            clean_summary += "."
        pieces.append(clean_summary)

    # 3. Main paragraphs
    raw_content = (article.content or "").strip()
    if raw_content:
        paragraphs = [p.strip() for p in raw_content.split("\n") if p.strip()]

        boilerplate_patterns = [
            re.compile(r"^\(?\s*(ảnh|nguồn|theo|video|đồ họa|bài và ảnh|thực hiện)[:\s]", re.IGNORECASE),
            re.compile(r"\b(hotline|email|bản quyền thuộc|mọi ý kiến đóng góp|xem thêm|bình luận)\b", re.IGNORECASE),
            re.compile(r"^[A-ZĐÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴ\s]{2,30}\s*(\(theo|\(ttxvn|\(vna)?$", re.IGNORECASE),
        ]

        cleaned_paragraphs = []
        for p in paragraphs:
            if any(pattern.search(p) for pattern in boilerplate_patterns):
                continue
            # Remove trailing credits like "(Theo VnExpress)" or "(Ảnh: TTXVN)"
            p = re.sub(
                r"\s*\([^\)]*(?:theo|ảnh|nguồn|ttxvn|vna|zing)[^\)]*\)\s*$",
                "",
                p,
                flags=re.IGNORECASE,
            ).strip()
            if len(p) >= 20:
                if not p.endswith((".", "!", "?", ":")):
                    p += "."
                cleaned_paragraphs.append(p)

        if mode == "full":
            pieces.extend(cleaned_paragraphs)
        else:
            current_words = sum(len(piece.split()) for piece in pieces)
            for p in cleaned_paragraphs:
                p_words = len(p.split())
                if current_words + p_words > max_words and current_words >= 80:
                    break
                pieces.append(p)
                current_words += p_words

    narration = "\n\n".join(pieces)
    return narration

