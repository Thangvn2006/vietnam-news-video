import os
from fastapi import Depends, HTTPException, Request
from loguru import logger

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.schema import (
    ArticleImageInfo,
    ArticleScrapeData,
    ArticleScrapeRequest,
    ArticleScrapeResponse,
)
from app.services import article_scraper
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])


@router.post(
    "/article/scrape",
    response_model=ArticleScrapeResponse,
    summary="Scrape news article for video generation",
)
def scrape_news_article(request: Request, body: ArticleScrapeRequest):
    """
    Extracts title, summary, content, publication metadata, and images from a news article URL.
    Optionally downloads images into storage/local_videos for immediate video material generation,
    and returns an optimized Google Gemini prompt for creating short news videos.
    """
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Article URL is required")

    if not article_scraper.is_safe_url(url):
        raise HTTPException(
            status_code=400,
            detail="Invalid or restricted URL (SSRF safety check failed)",
        )

    try:
        scraped = article_scraper.scrape_article(url)
    except Exception as e:
        logger.error(f"Failed to scrape article from {url}: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch or parse news article: {str(e)}",
        )

    downloaded_paths = []
    if body.download_images and scraped.images:
        local_materials_dir = utils.storage_dir("local_videos", create=True)
        try:
            downloaded_paths = article_scraper.download_article_images(
                scraped.images,
                output_dir=local_materials_dir,
                max_images=body.max_images,
            )
        except Exception as e:
            logger.warning(f"Error downloading article images: {e}")

    gemini_prompt = article_scraper.generate_gemini_news_prompt(
        scraped,
        language="vi" if any(c in scraped.title for c in "àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ") else "auto",
    )

    image_models = [
        ArticleImageInfo(
            url=img.url,
            alt=img.alt,
            caption=img.caption,
            local_path=getattr(img, "local_path", None),
        )
        for img in scraped.images
    ]

    scrape_data = ArticleScrapeData(
        title=scraped.title,
        summary=scraped.summary,
        content=scraped.content,
        domain=scraped.domain,
        url=scraped.url,
        authors=scraped.authors,
        publish_date=scraped.publish_date,
        images=image_models,
        downloaded_images=downloaded_paths,
        gemini_prompt=gemini_prompt,
        reading_script=article_scraper.build_article_reading_script(scraped),
    )

    return utils.get_response(200, scrape_data.model_dump())
