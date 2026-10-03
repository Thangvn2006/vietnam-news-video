import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.asgi import app
from app.services.article_scraper import ScrapedArticle, ScrapedImage


class TestArticleController(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("app.controllers.v1.article.article_scraper.scrape_article")
    def test_scrape_article_endpoint(self, mock_scrape):
        mock_scrape.return_value = ScrapedArticle(
            title="Đột phá công nghệ AI mới",
            summary="Các nhà khoa học vừa ra mắt mô hình AI thông minh mới.",
            content="Mô hình AI mới có khả năng xử lý ngôn ngữ tự nhiên và hình ảnh siêu việt.",
            url="https://vnexpress.net/dot-pha-cong-nghe-ai-moi.html",
            domain="vnexpress.net",
            authors=["Tuấn Anh"],
            publish_date="2026-10-03",
            images=[
                ScrapedImage(
                    url="https://vnexpress.net/images/ai-breakthrough.jpg",
                    caption="Mô hình AI trong phòng thí nghiệm",
                )
            ],
        )

        response = self.client.post(
            "/api/v1/article/scrape",
            json={
                "url": "https://vnexpress.net/dot-pha-cong-nghe-ai-moi.html",
                "download_images": False,
            },
        )
        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertEqual(json_data["status"], 200)
        data = json_data["data"]
        self.assertEqual(data["title"], "Đột phá công nghệ AI mới")
        self.assertEqual(data["domain"], "vnexpress.net")
        self.assertIn("Mô hình AI mới", data["content"])
        self.assertIn("gemini_prompt", data)
        self.assertIn("Đột phá công nghệ AI mới", data["gemini_prompt"])
        self.assertEqual(len(data["images"]), 1)

    def test_scrape_article_endpoint_invalid_url(self):
        # SSRF blocked local IP
        response = self.client.post(
            "/api/v1/article/scrape",
            json={"url": "http://127.0.0.1:8000/private"},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
