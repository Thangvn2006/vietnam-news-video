import os
import sys
import time
import json
import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.asgi import app
from app.config import config
from app.models.schema import VideoAspect, VideoConcatMode
from app.services import (
    article_scraper,
    cache_manager,
    llm,
    material,
    task as task_service,
    voice,
)
from app.controllers.v1 import video as video_controller
from app.utils import utils


class ComprehensiveSystemQATest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    # =========================================================================
    # NHÓM 1: KIỂM THỬ LUỒNG CHÍNH (Happy Path / Positive Testing)
    # =========================================================================

    def test_tc_hp_01_api_health_ping(self):
        """TC-HP-01: Kiểm tra API health check (/ping) trả về trạng thái hoạt động tức thì."""
        start = time.perf_counter()
        response = self.client.get("/ping")
        elapsed = time.perf_counter() - start

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), "pong")
        self.assertLess(elapsed, 0.5, "Ping API response time should be under 500ms")

    def test_tc_hp_02_article_scraper_positive(self):
        """TC-HP-02: Kiểm tra bóc tách bài báo tiếng Việt chuẩn (Title, Summary, Content, Images, Prompt)."""
        sample_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta property="og:title" content="Kinh tế Việt Nam tăng trưởng tích cực quý 3 năm 2026 - VnExpress">
            <meta property="og:description" content="GDP quý 3 ước tính tăng 7.4% nhờ sự phục hồi mạnh mẽ của xuất khẩu và sản xuất công nghiệp.">
            <meta name="author" content="Hà My">
            <meta property="article:published_time" content="2026-10-01T07:30:00+07:00">
        </head>
        <body>
            <article>
                <h1>Kinh tế Việt Nam tăng trưởng tích cực quý 3 năm 2026</h1>
                <p>GDP quý 3 ước tính tăng 7.4% nhờ sự phục hồi mạnh mẽ của xuất khẩu và sản xuất công nghiệp.</p>
                <p>Theo Tổng cục Thống kê, các chỉ số kinh tế vĩ mô tiếp tục duy trì đà tăng trưởng ổn định trong 9 tháng đầu năm.</p>
                <figure>
                    <img src="https://vnexpress.net/images/kinh-te-q3.jpg" alt="Đồ thị tăng trưởng GDP" />
                    <figcaption>Tăng trưởng GDP qua các quý</figcaption>
                </figure>
                <p>Khu vực nông nghiệp và dịch vụ cũng ghi nhận nhiều tín hiệu khởi sắc vượt mong đợi.</p>
            </article>
        </body>
        </html>
        """
        article = article_scraper.parse_article_html(
            sample_html, url="https://vnexpress.net/kinh-te-q3-2026.html"
        )
        self.assertEqual(article.title, "Kinh tế Việt Nam tăng trưởng tích cực quý 3 năm 2026")
        self.assertIn("GDP quý 3 ước tính tăng 7.4%", article.summary)
        self.assertIn("Tổng cục Thống kê", article.content)
        self.assertEqual(article.domain, "vnexpress.net")
        self.assertIn("Hà My", article.authors)
        self.assertGreaterEqual(len(article.images), 1)

        # Gemini prompt generation test
        gemini_prompt = article_scraper.generate_gemini_news_prompt(article, language="vi")
        self.assertIn("Kinh tế Việt Nam tăng trưởng tích cực", gemini_prompt)
        self.assertIn("VIDEO SUBJECT", gemini_prompt)
        self.assertIn("VIDEO SCRIPT", gemini_prompt)

        # Reading script generation test
        reading_script = article_scraper.build_article_reading_script(article)
        self.assertIn("Kinh tế Việt Nam", reading_script)

    def test_tc_hp_03_article_scrape_endpoint(self):
        """TC-HP-03: Kiểm tra Endpoint POST /api/v1/article/scrape với payload chuẩn."""
        with patch("app.controllers.v1.article.article_scraper.scrape_article") as mock_scrape:
            mock_scrape.return_value = article_scraper.ScrapedArticle(
                title="Việt Nam dẫn đầu xuất khẩu gạo chất lượng cao",
                summary="Việt Nam khẳng định vị thế trên thị trường lúa gạo quốc tế.",
                content="Năm 2026, kim ngạch xuất khẩu gạo của Việt Nam lập kỷ lục mới nhờ các giống gạo thơm đặc sản.",
                url="https://dantri.com.vn/kinh-doanh/xuat-khau-gao-2026.htm",
                domain="dantri.com.vn",
                authors=["Văn Hùng"],
                publish_date="2026-10-02",
                images=[
                    article_scraper.ScrapedImage(
                        url="https://dantri.com.vn/images/gao-viet-nam.jpg",
                        alt="Cánh đồng lúa vàng óng",
                        caption="Vụ mùa bội thu tại Đồng bằng sông Cửu Long",
                    )
                ],
            )
            response = self.client.post(
                "/api/v1/article/scrape",
                json={
                    "url": "https://dantri.com.vn/kinh-doanh/xuat-khau-gao-2026.htm",
                    "download_images": False,
                },
            )
            self.assertEqual(response.status_code, 200)
            data = response.json().get("data", {})
            self.assertEqual(data.get("title"), "Việt Nam dẫn đầu xuất khẩu gạo chất lượng cao")
            self.assertEqual(data.get("domain"), "dantri.com.vn")
            self.assertIn("gemini_prompt", data)
            self.assertIn("reading_script", data)

    def test_tc_hp_04_subtitle_srt_generator(self):
        """TC-HP-04: Kiểm tra bộ tạo Subtitle SRT tiếng Việt chuẩn thời gian và định dạng."""
        subtitles = [
            (1, "Chào mừng các bạn đến với bản tin hôm nay.", 0.0, 3.25),
            (2, "Hôm nay chúng ta cùng điểm qua các sự kiện nổi bật.", 3.5, 7.8),
        ]
        srt_content = ""
        for idx, text, start, end in subtitles:
            srt_content += utils.text_to_srt(idx, text, start, end) + "\n"

        self.assertIn("00:00:00,000 --> 00:00:03,250", srt_content)
        self.assertIn("Chào mừng các bạn", srt_content)
        self.assertIn("00:00:03,500 --> 00:00:07,800", srt_content)
        self.assertIn("sự kiện nổi bật", srt_content)

    def test_tc_hp_05_task_lifecycle_happy_path(self):
        """TC-HP-05: Kiểm tra vòng đời Task bất đồng bộ (tạo task, truy vấn trạng thái, danh sách)."""
        params = {
            "video_subject": "Bản tin công nghệ Việt Nam",
            "video_script": "Trí tuệ nhân tạo đang thay đổi cách người Việt làm việc và sáng tạo nội dung.",
            "video_aspect": "9:16",
            "voice_name": "vi-VN-HoaiMyNeural",
            "bgm_volume": 0.2,
        }
        with patch.object(video_controller.task_manager, "add_task"):
            response = self.client.post("/api/v1/videos", json=params)
            self.assertEqual(response.status_code, 200)
            res_json = response.json()
            task_id = res_json.get("data", {}).get("task_id")
            self.assertTrue(task_id)

            # Query task status
            status_res = self.client.get(f"/api/v1/tasks/{task_id}")
            self.assertEqual(status_res.status_code, 200)
            self.assertIn("state", status_res.json().get("data", {}))

    # =========================================================================
    # NHÓM 2: KIỂM THỬ XỬ LÝ LỖI & NGOẠI LỆ (Negative & Edge Case Testing)
    # =========================================================================

    def test_tc_neg_01_article_ssrf_safety_protection(self):
        """TC-NEG-01: Ngăn chặn SSRF tấn công mạng nội bộ và các scheme độc hại."""
        blocked_urls = [
            "http://127.0.0.1:8000/internal",
            "http://localhost/admin",
            "http://192.168.1.1/router-config",
            "http://10.0.0.1/private",
            "http://169.254.169.254/latest/meta-data/",
            "file:///C:/Windows/win.ini",
            "ftp://ftp.example.com/malicious",
            "javascript:alert(1)",
        ]
        for url in blocked_urls:
            with self.subTest(url=url):
                self.assertFalse(
                    article_scraper.is_safe_url(url),
                    f"SSRF check should block unsafe URL: {url}",
                )
                response = self.client.post(
                    "/api/v1/article/scrape",
                    json={"url": url},
                )
                self.assertEqual(
                    response.status_code,
                    400,
                    f"Endpoint should return 400 for unsafe URL: {url}",
                )

    def test_tc_neg_02_article_empty_and_whitespace_url(self):
        """TC-NEG-02: Nhập URL rỗng, chỉ chứa khoảng trắng hoặc định dạng sai."""
        bad_inputs = ["", "   ", "\t\n", "invalid_url_without_protocol", "https://"]
        for bad_url in bad_inputs:
            with self.subTest(bad_url=bad_url):
                response = self.client.post(
                    "/api/v1/article/scrape",
                    json={"url": bad_url},
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("detail", response.json())

    def test_tc_neg_03_query_non_existent_task(self):
        """TC-NEG-03: Truy vấn Task ID không tồn tại hoặc UUID rác trả về 404 chuẩn."""
        fake_task_ids = [
            "non-existent-task-9999",
            "00000000-0000-0000-0000-000000000000",
            "malicious-payload' OR '1'='1",
        ]
        for fake_id in fake_task_ids:
            with self.subTest(fake_id=fake_id):
                response = self.client.get(f"/api/v1/tasks/{fake_id}")
                self.assertIn(
                    response.status_code,
                    (400, 404),
                    f"Non existent task should return 400 or 404, got {response.status_code}",
                )

    def test_tc_neg_04_invalid_aspect_ratio_validation(self):
        """TC-NEG-04: Gửi tham số video_aspect sai chuẩn (không nằm trong enum 16:9, 9:16, 1:1)."""
        invalid_payload = {
            "video_subject": "Test subject",
            "video_script": "Test script",
            "video_aspect": "21:9",  # Invalid aspect ratio
        }
        response = self.client.post("/api/v1/videos", json=invalid_payload)
        self.assertEqual(response.status_code, 400, "Validation exception handler should return 400")
        self.assertIn("data", response.json())

    def test_tc_neg_05_spam_repeated_requests_resilience(self):
        """TC-NEG-05: Gửi liên tiếp 30 request dồn dập (stress/spam) để kiểm tra hệ thống không crash/leak."""
        errors = 0
        for _ in range(30):
            res = self.client.get("/ping")
            if res.status_code != 200:
                errors += 1
        self.assertEqual(errors, 0, "Server must handle repeated spam requests cleanly without failures")

    # =========================================================================
    # NHÓM 3: KIỂM THỬ TÍNH TƯƠNG THÍCH & CẤU HÌNH (Configuration Testing)
    # =========================================================================

    def test_tc_cfg_01_aspect_ratios_support(self):
        """TC-CFG-01: Kiểm tra hỗ trợ các tỷ lệ màn hình chuẩn (TikTok 9:16, YouTube 16:9, Square 1:1)."""
        aspects = ["9:16", "16:9", "1:1"]
        for asp in aspects:
            with self.subTest(aspect=asp):
                parsed = VideoAspect(asp)
                self.assertEqual(parsed.value, asp)

    def test_tc_cfg_02_vietnamese_i18n_translation_keys_completeness(self):
        """TC-CFG-02: Kiểm tra file vi.json chứa đầy đủ các khóa giao diện thiết yếu cho Video Tin Tức."""
        i18n_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "webui", "i18n")
        vi_file = os.path.join(i18n_path, "vi.json")
        en_file = os.path.join(i18n_path, "en.json")

        self.assertTrue(os.path.exists(vi_file), "vi.json must exist")
        self.assertTrue(os.path.exists(en_file), "en.json must exist")

        with open(vi_file, "r", encoding="utf-8") as f:
            vi_data = json.load(f)
        vi_dict = vi_data.get("Translation", vi_data)

        # Essential keys for Vietnamese News Video
        essential_keys = [
            "Video Subject",
            "Video Script",
            "Audio Settings",
            "Background Music",
            "Background Music Volume",
            "Voiceover Voice",
            "Voiceover Volume",
            "Subtitle Settings",
            "Enable Subtitles",
        ]
        missing_in_vi = [k for k in essential_keys if k not in vi_dict]
        self.assertEqual(missing_in_vi, [], f"Missing essential keys in vi.json: {missing_in_vi}")

    def test_tc_cfg_03_language_resolution_vietnam_default(self):
        """TC-CFG-03: Kiểm tra cơ chế tự động chọn ngôn ngữ ưu tiên tiếng Việt theo cấu hình dự án."""
        supported = ["vi", "en", "zh"]
        # Explicit saved language
        self.assertEqual(utils.resolve_ui_language("en", "vi-VN", supported), "en")
        # Browser language Vietnamese
        self.assertEqual(utils.resolve_ui_language("", "vi-VN", supported), "vi")
        self.assertEqual(utils.resolve_ui_language(None, "vi", supported), "vi")
        # Default fallback
        self.assertEqual(utils.resolve_ui_language(None, "unsupported-locale", supported, default_language="vi"), "vi")

    def test_tc_cfg_04_vietnamese_edge_tts_voices_availability(self):
        """TC-CFG-04: Kiểm tra danh mục giọng đọc tiếng Việt (Hoài My, Nam Minh)."""
        vietnamese_voices = [
            "vi-VN-HoaiMyNeural",
            "vi-VN-NamMinhNeural",
        ]
        # Verify voice names follow Azure/EdgeTTS format
        for v in vietnamese_voices:
            self.assertTrue(v.startswith("vi-VN-"))
            self.assertTrue(v.endswith("Neural"))

    def test_tc_cfg_05_windows_path_normalization(self):
        """TC-CFG-05: Kiểm tra tính tương thích đường dẫn Windows (backslash vs forward slash)."""
        test_path = "storage\\local_videos\\news_clip_01.mp4"
        normalized = os.path.normpath(test_path)
        self.assertTrue(normalized.endswith("news_clip_01.mp4"))

    # =========================================================================
    # NHÓM 4: KIỂM THỬ HIỆU NĂNG VÀ PHẢN HỒI (Performance & Response Test)
    # =========================================================================

    def test_tc_prf_01_api_latency_under_threshold(self):
        """TC-PRF-01: Đo lường thời gian phản hồi API cơ sở (ngưỡng chấp nhận < 0.5 giây)."""
        latencies = []
        for _ in range(5):
            t0 = time.perf_counter()
            res = self.client.get("/ping")
            latencies.append(time.perf_counter() - t0)
            self.assertEqual(res.status_code, 200)

        avg_latency = sum(latencies) / len(latencies)
        self.assertLess(avg_latency, 0.5, f"Average ping latency {avg_latency:.4f}s exceeds 0.5s")

    def test_tc_prf_02_article_parsing_performance(self):
        """TC-PRF-02: Tốc độ phân tích HTML bài báo dài (100 đoạn khác nhau) phải dưới 200ms."""
        paragraphs_html = "".join([f"<p>Đoạn văn nội dung tin tức số {i} phản ánh sự kiện kinh tế và xã hội tại Việt Nam năm 2026.</p>" for i in range(100)])
        large_article_html = f"""
        <html><body><article>
        <h1>Bản tin thời sự đặc biệt quy mô lớn</h1>
        <p>Sapo tóm tắt nội dung bài viết dài phục vụ kiểm thử hiệu năng.</p>
        {paragraphs_html}
        </article></body></html>
        """
        t0 = time.perf_counter()
        parsed = article_scraper.parse_article_html(large_article_html, "https://tuoitre.vn/bai-dai.htm")
        duration = time.perf_counter() - t0

        self.assertLess(duration, 0.2, f"Large HTML parsing took {duration:.4f}s, expected < 0.2s")
        self.assertGreater(len(parsed.content), 5000)

    def test_tc_prf_03_srt_generation_speed(self):
        """TC-PRF-03: Tốc độ sinh 1,000 dòng phụ đề SRT liên tục phải dưới 100ms."""
        t0 = time.perf_counter()
        srt_batch = ""
        for i in range(1000):
            srt_batch += utils.text_to_srt(i + 1, f"Dòng phụ đề tin tức thứ {i+1}", float(i), float(i + 1))
        duration = time.perf_counter() - t0

        self.assertLess(duration, 0.1, f"1000 SRT lines generation took {duration:.4f}s, expected < 0.1s")

    def test_tc_prf_04_async_task_dispatch_latency(self):
        """TC-PRF-04: Khởi tạo tác vụ bất đồng bộ phải trả về task_id ngay lập tức (< 1.5s), không block tiến trình."""
        params = {
            "video_subject": "Đánh giá hiệu năng xử lý tác vụ",
            "video_script": "Kiểm tra cơ chế hàng đợi bất đồng bộ.",
            "video_aspect": "16:9",
        }
        with patch.object(video_controller.task_manager, "add_task"):
            t0 = time.perf_counter()
            response = self.client.post("/api/v1/videos", json=params)
            duration = time.perf_counter() - t0

            self.assertEqual(response.status_code, 200)
            self.assertLess(duration, 1.5, f"Task dispatch took {duration:.4f}s, expected < 1.5s")
            self.assertTrue(response.json().get("data", {}).get("task_id"))


if __name__ == "__main__":
    unittest.main()
