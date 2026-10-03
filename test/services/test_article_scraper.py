import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app.services.article_scraper import (
    ScrapedArticle,
    ScrapedImage,
    _clean_title,
    build_article_reading_script,
    download_article_images,
    generate_gemini_news_prompt,
    is_safe_url,
    parse_article_html,
)


class TestArticleScraper(unittest.TestCase):
    def test_is_safe_url(self):
        # Valid URLs
        self.assertTrue(is_safe_url("https://vnexpress.net/thoi-su-4000.html"))
        self.assertTrue(is_safe_url("http://dantri.com.vn/kinh-doanh/bai-viet.htm"))
        self.assertTrue(is_safe_url("https://tuoitre.vn/tin-tuc-moi.htm"))

        # Invalid or unsafe URLs (SSRF protection)
        self.assertFalse(is_safe_url("ftp://example.com/file"))
        self.assertFalse(is_safe_url("file:///etc/passwd"))
        self.assertFalse(is_safe_url("http://localhost:8080/admin"))
        self.assertFalse(is_safe_url("http://127.0.0.1:8000/api"))
        self.assertFalse(is_safe_url("http://192.168.1.1/secret"))
        self.assertFalse(is_safe_url("http://10.0.0.1/internal"))
        self.assertFalse(is_safe_url("not-a-url"))

    def test_clean_title(self):
        self.assertEqual(
            _clean_title("Kinh tế Việt Nam tăng trưởng vượt dự báo - VnExpress"),
            "Kinh tế Việt Nam tăng trưởng vượt dự báo",
        )
        self.assertEqual(
            _clean_title("Khởi tố vụ án lừa đảo | Báo Dân trí"),
            "Khởi tố vụ án lừa đảo",
        )
        self.assertEqual(
            _clean_title("Thời tiết hôm nay – Tuổi Trẻ Online"),
            "Thời tiết hôm nay",
        )
        self.assertEqual(
            _clean_title("Tiêu đề bài viết thông thường"),
            "Tiêu đề bài viết thông thường",
        )

    def test_parse_article_html(self):
        sample_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta property="og:title" content="Hà Nội khánh thành cầu mới - Báo Tuổi Trẻ">
            <meta property="og:description" content="Cây cầu mới bắc qua sông Hồng vừa chính thức thông xe sáng nay.">
            <meta property="og:image" content="https://tuoitre.vn/images/cover.jpg">
            <meta name="author" content="Nguyễn Văn A">
            <meta property="article:published_time" content="2026-10-03T08:00:00Z">
        </head>
        <body>
            <article class="detail-content">
                <h1 class="title">Hà Nội khánh thành cầu mới</h1>
                <p class="sapo">Cây cầu mới bắc qua sông Hồng vừa chính thức thông xe sáng nay.</p>
                <div class="advertisement">Quảng cáo độc quyền ở đây</div>
                <script>console.log("bad script");</script>
                <p>Sáng 3/10, công trình cầu vượt sông Hồng với tổng vốn đầu tư hàng nghìn tỷ đồng đã chính thức được khánh thành.</p>
                <figure>
                    <img data-src="/images/cau-moi.jpg" alt="Toàn cảnh cây cầu mới khánh thành">
                    <figcaption>Cây cầu được hoàn thành sau 2 năm thi công</figcaption>
                </figure>
                <p>Dự án hoàn thành đúng tiến độ, góp phần giảm tải ùn tắc giao thông nghiêm trọng tại cửa ngõ phía Đông Thủ đô.</p>
                <div class="social-share">Chia sẻ bài viết qua Facebook</div>
            </article>
        </body>
        </html>
        """
        article = parse_article_html(sample_html, url="https://tuoitre.vn/ha-noi-khanh-thanh-cau-moi.htm")

        self.assertEqual(article.title, "Hà Nội khánh thành cầu mới")
        self.assertEqual(article.summary, "Cây cầu mới bắc qua sông Hồng vừa chính thức thông xe sáng nay.")
        self.assertEqual(article.domain, "tuoitre.vn")
        self.assertIn("Nguyễn Văn A", article.authors)
        self.assertEqual(article.publish_date, "2026-10-03T08:00:00Z")

        # Verify content has paragraphs and no advertising/scripts
        self.assertIn("Sáng 3/10, công trình cầu vượt sông Hồng", article.content)
        self.assertIn("Dự án hoàn thành đúng tiến độ", article.content)
        self.assertNotIn("Quảng cáo độc quyền", article.content)
        self.assertNotIn("bad script", article.content)

        # Verify images
        self.assertGreaterEqual(len(article.images), 2)
        cover_img = article.images[0]
        self.assertEqual(cover_img.url, "https://tuoitre.vn/images/cover.jpg")
        body_img = article.images[1]
        self.assertEqual(body_img.url, "https://tuoitre.vn/images/cau-moi.jpg")
        self.assertEqual(body_img.caption, "Cây cầu được hoàn thành sau 2 năm thi công")

    def test_generate_gemini_news_prompt(self):
        article = ScrapedArticle(
            url="https://vnexpress.net/test.html",
            title="Giá vàng hôm nay tăng mạnh",
            summary="Giá vàng thế giới và trong nước đồng loạt lập đỉnh mới sáng nay.",
            content="Giá vàng hôm nay tiếp tục ghi nhận đà tăng phi mã do những bất ổn địa chính trị...",
            domain="vnexpress.net",
        )
        prompt = generate_gemini_news_prompt(article, target_duration=45)
        self.assertIn("Giá vàng hôm nay tăng mạnh", prompt)
        self.assertIn("45 giây", prompt)
        self.assertIn("VIDEO SUBJECT", prompt)
        self.assertIn("VIDEO SCRIPT", prompt)
        self.assertIn("VIDEO SEARCH TERMS", prompt)
        self.assertIn("IMAGE PROMPTS", prompt)

    def test_download_article_images(self):
        import io
        from PIL import Image

        # Create a real 400x300 in-memory JPEG image for mock response
        img_buffer = io.BytesIO()
        test_img = Image.new("RGB", (400, 300), color="blue")
        test_img.save(img_buffer, format="JPEG")
        fake_image_bytes = img_buffer.getvalue()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "image/jpeg"}
        mock_resp.content = fake_image_bytes

        with tempfile.TemporaryDirectory() as tmp_dir, patch("requests.get", return_value=mock_resp):
            images = [
                ScrapedImage(url="https://example.com/test1.jpg", alt="test 1"),
                ScrapedImage(url="https://example.com/test2.jpg", alt="test 2"),
            ]
            downloaded = download_article_images(images, save_dir=tmp_dir, max_images=2)
            self.assertEqual(len(downloaded), 2)
            for path in downloaded:
                self.assertTrue(os.path.exists(path))
                self.assertGreater(os.path.getsize(path), 0)

            # Test output_dir parameter alias
            downloaded_output_dir = download_article_images(images, output_dir=tmp_dir, max_images=1)
            self.assertEqual(len(downloaded_output_dir), 1)
            self.assertTrue(os.path.exists(downloaded_output_dir[0]))


    def test_build_article_reading_script(self):
        article = ScrapedArticle(
            title="Hà Nội khánh thành cầu mới qua sông Hồng",
            summary="Cây cầu mới vừa chính thức thông xe sáng nay.",
            content=(
                "Sáng ngày 3/10, công trình cầu vượt sông Hồng chính thức được đưa vào hoạt động sau 2 năm thi công khẩn trương.\n\n"
                "Dự án hoàn thành đúng tiến độ, góp phần giảm tải ùn tắc giao thông nghiêm trọng tại cửa ngõ phía Đông Thủ đô.\n\n"
                "(Ảnh: TTXVN)"
            ),
            url="https://tuoitre.vn/cau-moi.htm",
            domain="tuoitre.vn",
        )
        narration = build_article_reading_script(article, max_words=100, mode="concise")
        self.assertIn("Hà Nội khánh thành cầu mới", narration)
        self.assertIn("Cây cầu mới vừa chính thức thông xe sáng nay", narration)
        self.assertIn("Sáng ngày 3/10", narration)
        self.assertNotIn("Ảnh: TTXVN", narration)


if __name__ == "__main__":
    unittest.main()
