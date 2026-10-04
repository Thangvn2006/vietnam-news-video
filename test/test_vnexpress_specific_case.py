import os
import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.asgi import app
from app.controllers.v1 import video as video_controller
from app.services import article_scraper
from app.utils import utils

TARGET_VNEXPRESS_URL = (
    "https://vnexpress.net/31-nguoi-viet-truong-thanh-dang-ngoi-qua-nhieu-van-dong-qua-it-5127891.html"
)


class TestVnExpressLiveArticleQA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.url = TARGET_VNEXPRESS_URL

    # =========================================================================
    # 1. KIỂM THỬ LUỒNG CHÍNH (Happy Path / Positive Testing)
    # =========================================================================

    def test_tc_vne_hp_01_live_article_scrape(self):
        """TC-VNE-HP-01: Cào trực tiếp bài báo VnExpress và kiểm tra tính toàn vẹn của dữ liệu."""
        self.assertTrue(article_scraper.is_safe_url(self.url))

        t0 = time.perf_counter()
        article = article_scraper.scrape_article(self.url)
        scrape_duration = time.perf_counter() - t0

        # Kiểm tra tiêu đề chính xác
        self.assertEqual(
            article.title,
            "31% người Việt trưởng thành đang 'ngồi quá nhiều, vận động quá ít'",
        )
        self.assertEqual(article.domain, "vnexpress.net")
        self.assertIn("Khoảng 30% người Việt trưởng thành thiếu vận động thể lực", article.summary)
        self.assertIn("Tổ chức Y tế Thế giới (WHO)", article.content)
        self.assertIn("bộ y tế", article.content.lower())
        self.assertGreater(len(article.content), 2000)
        self.assertGreaterEqual(len(article.images), 1)
        self.assertLess(scrape_duration, 3.0, f"Live scrape took {scrape_duration:.3f}s, expected < 3.0s")

    def test_tc_vne_hp_02_script_and_prompt_generation(self):
        """TC-VNE-HP-02: Kiểm tra sinh kịch bản đọc tin 60s và prompt Gemini từ bài báo VnExpress."""
        article = article_scraper.scrape_article(self.url)

        # Sinh prompt cho Google Gemini
        prompt = article_scraper.generate_gemini_news_prompt(article, target_duration=60, language="vi")
        self.assertIn("31% người Việt trưởng thành", prompt)
        self.assertIn("VIDEO SUBJECT", prompt)
        self.assertIn("VIDEO SCRIPT", prompt)
        self.assertIn("VIDEO SEARCH TERMS", prompt)
        self.assertIn("IMAGE PROMPTS", prompt)

        # Sinh kịch bản đọc tin tiếng Việt chuẩn hóa
        reading_script = article_scraper.build_article_reading_script(article)
        word_count = len(reading_script.split())
        self.assertGreaterEqual(word_count, 150)
        self.assertLessEqual(word_count, 350)
        self.assertIn("31% người Việt trưởng thành", reading_script)

    def test_tc_vne_hp_03_api_scrape_with_image_download(self):
        """TC-VNE-HP-03: Kiểm tra API POST /api/v1/article/scrape tải và lưu trữ ảnh minh họa hợp lệ."""
        response = self.client.post(
            "/api/v1/article/scrape",
            json={
                "url": self.url,
                "download_images": True,
                "max_images": 2,
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json().get("data", {})

        self.assertEqual(data.get("title"), "31% người Việt trưởng thành đang 'ngồi quá nhiều, vận động quá ít'")
        self.assertEqual(data.get("domain"), "vnexpress.net")
        self.assertTrue(data.get("gemini_prompt"))
        self.assertTrue(data.get("reading_script"))

        downloaded_images = data.get("downloaded_images", [])
        self.assertGreaterEqual(len(downloaded_images), 1)
        for img_path in downloaded_images:
            self.assertTrue(os.path.exists(img_path), f"Downloaded image file must exist: {img_path}")
            self.assertGreater(os.path.getsize(img_path), 5000)

    def test_tc_vne_hp_04_e2e_video_task_creation(self):
        """TC-VNE-HP-04: Tích hợp dữ liệu bài báo VnExpress vào quy trình tạo tác vụ Video (Task Creation)."""
        article = article_scraper.scrape_article(self.url)
        reading_script = article_scraper.build_article_reading_script(article)

        params = {
            "video_subject": article.title,
            "video_script": reading_script,
            "video_aspect": "9:16",
            "voice_name": "vi-VN-HoaiMyNeural",
            "bgm_volume": 0.2,
        }

        with patch.object(video_controller.task_manager, "add_task") as mock_add:
            response = self.client.post("/api/v1/videos", json=params)
            self.assertEqual(response.status_code, 200)
            data = response.json().get("data", {})
            task_id = data.get("task_id")
            self.assertTrue(task_id)
            self.assertTrue(mock_add.called)

            # Truy vấn lại trạng thái task
            query_res = self.client.get(f"/api/v1/tasks/{task_id}")
            self.assertEqual(query_res.status_code, 200)
            self.assertEqual(query_res.json().get("status"), 200)

    # =========================================================================
    # 2. KIỂM THỬ XỬ LÝ LỖI & NGOẠI LỆ (Negative & Edge Case Testing)
    # =========================================================================

    def test_tc_vne_neg_01_url_variants_and_anchor(self):
        """TC-VNE-NEG-01: Xử lý URL VnExpress kèm hash anchor (#box_comment) hoặc khoảng trắng."""
        anchor_url = f"{self.url}#box_comment_vne"
        res_anchor = self.client.post("/api/v1/article/scrape", json={"url": anchor_url})
        self.assertEqual(res_anchor.status_code, 200)
        self.assertIn("31%", res_anchor.json().get("data", {}).get("title"))

        spaced_url = f"   {self.url}   \n"
        res_space = self.client.post("/api/v1/article/scrape", json={"url": spaced_url})
        self.assertEqual(res_space.status_code, 200)
        self.assertIn("31%", res_space.json().get("data", {}).get("title"))

    def test_tc_vne_neg_02_url_with_tracking_query_params(self):
        """TC-VNE-NEG-02: Xử lý URL VnExpress kèm tham số tiếp thị UTM quảng cáo."""
        utm_url = f"{self.url}?utm_source=facebook&utm_medium=cpc&utm_campaign=qa_test"
        res_utm = self.client.post("/api/v1/article/scrape", json={"url": utm_url})
        self.assertEqual(res_utm.status_code, 200)
        self.assertIn("31%", res_utm.json().get("data", {}).get("title"))

    def test_tc_vne_neg_03_special_characters_in_article_title(self):
        """TC-VNE-NEG-03: Kiểm tra ký tự đặc biệt: số %, dấu nháy đơn, dấu phẩy trong tiêu đề và nội dung."""
        article = article_scraper.scrape_article(self.url)
        self.assertIn("%", article.title)
        self.assertIn("'", article.title)

        # Đảm bảo không bị vỡ chuỗi JSON hay escape lỗi
        serialized = utils.to_json(article.__dict__)
        self.assertIsNotNone(serialized)
        self.assertIn("31%", serialized)

    # =========================================================================
    # 3. KIỂM THỬ HIỆU NĂNG VÀ PHẢN HỒI (Performance & Response Test)
    # =========================================================================

    def test_tc_vne_prf_01_performance_benchmarks(self):
        """TC-VNE-PRF-01: Đo lường thời gian cào bài báo thực tế, sinh kịch bản và khởi tạo task."""
        # 1. Scrape latency
        t0 = time.perf_counter()
        article = article_scraper.scrape_article(self.url)
        scrape_time = time.perf_counter() - t0
        self.assertLess(scrape_time, 2.5, f"Network scrape took {scrape_time:.3f}s")

        # 2. Script generation latency
        t1 = time.perf_counter()
        script = article_scraper.build_article_reading_script(article)
        script_time = time.perf_counter() - t1
        self.assertLess(script_time, 0.05, f"Script build took {script_time:.4f}s")

        # 3. Prompt generation latency
        t2 = time.perf_counter()
        prompt = article_scraper.generate_gemini_news_prompt(article, target_duration=60, language="vi")
        prompt_time = time.perf_counter() - t2
        self.assertLess(prompt_time, 0.05, f"Prompt build took {prompt_time:.4f}s")


if __name__ == "__main__":
    unittest.main()
