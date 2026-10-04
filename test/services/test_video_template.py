import os
import unittest
from PIL import Image

from app.services import video_template
from app.services import article_scraper


class TestVideoTemplate(unittest.TestCase):
    def setUp(self):
        self.templates_dir = video_template.get_templates_dir()
        os.makedirs(self.templates_dir, exist_ok=True)

    def test_ensure_default_templates_exist(self):
        templates = video_template.ensure_default_templates_exist()
        self.assertIn("template_9_16_checkerboard.png", templates)
        self.assertIn("template_9_16_transparent.png", templates)
        self.assertIn("template_16_9_checkerboard.png", templates)
        self.assertIn("template_16_9_transparent.png", templates)

        for filename, filepath in templates.items():
            self.assertTrue(os.path.exists(filepath), f"File {filepath} should exist")
            with Image.open(filepath) as img:
                self.assertEqual(img.mode, "RGBA")

    def test_create_frame_template_checkerboard(self):
        img = video_template.create_frame_template(
            width=540,
            height=960,
            with_checkerboard=True,
            title="TIN NÓNG",
            source_text="VnExpress",
        )
        self.assertEqual(img.size, (540, 960))
        self.assertEqual(img.mode, "RGBA")
        # Center pixel should not be transparent (checkerboard / guide box)
        center_pixel = img.getpixel((270, 480))
        self.assertGreater(center_pixel[3], 0)


    def test_create_frame_template_transparent(self):
        img = video_template.create_frame_template(
            width=540,
            height=960,
            with_checkerboard=False,
            title="TIN NÓNG",
            source_text="VnExpress",
        )
        self.assertEqual(img.size, (540, 960))
        self.assertEqual(img.mode, "RGBA")
        # Center pixel should be 100% transparent
        center_pixel = img.getpixel((270, 480))
        self.assertEqual(center_pixel[3], 0)

    def test_create_source_badge_image_positions(self):
        positions = [
            "top_right",
            "top_center",
            "top_left",
            "bottom_right",
            "bottom_center",
            "bottom_left",
        ]
        for pos in positions:
            with self.subTest(position=pos):
                badge_img = video_template.create_source_badge_image(
                    text="Nguồn: Tuổi Trẻ",
                    width=720,
                    height=1280,
                    position=pos,
                )
                self.assertEqual(badge_img.size, (720, 1280))
                self.assertEqual(badge_img.mode, "RGBA")

    def test_get_available_templates(self):
        templates_portrait = video_template.get_available_templates("9:16")
        self.assertTrue(len(templates_portrait) >= 3)
        self.assertTrue(any(t["id"] == "none" for t in templates_portrait))
        self.assertTrue(any("template_9_16" in t["id"] for t in templates_portrait))

        templates_landscape = video_template.get_available_templates("16:9")
        self.assertTrue(len(templates_landscape) >= 3)
        self.assertTrue(any("template_16_9" in t["id"] for t in templates_landscape))

    def test_save_uploaded_template(self):
        dummy_png = Image.new("RGBA", (100, 100), (255, 0, 0, 128))
        import io
        buf = io.BytesIO()
        dummy_png.save(buf, format="PNG")
        saved_path = video_template.save_uploaded_template(buf.getvalue(), "custom_test_frame.png")
        self.assertTrue(os.path.exists(saved_path))
        self.assertTrue(saved_path.endswith("custom_test_frame.png"))

    def test_get_news_source_name(self):
        self.assertEqual(article_scraper.get_news_source_name("https://vnexpress.net/thoi-su"), "VnExpress")
        self.assertEqual(article_scraper.get_news_source_name("https://tuoitre.vn/tin-tuc"), "Tuổi Trẻ")
        self.assertEqual(article_scraper.get_news_source_name("https://dantri.com.vn/xa-hoi"), "Dân Trí")
        self.assertEqual(article_scraper.get_news_source_name("dantri.com.vn"), "Dân Trí")
        self.assertEqual(article_scraper.get_news_source_name("https://example.com/post"), "Example")
    def test_create_headline_banner_image(self):
        for pos in ["top", "center", "bottom"]:
            # Test without badge (clean title banner)
            img = video_template.create_headline_banner_image(
                headline_badge="",
                headline_title="Thủ tướng yêu cầu rà soát chương trình phổ cập",
                width=1080,
                height=1920,
                position=pos,
            )
            self.assertEqual(img.size, (1080, 1920))
            self.assertEqual(img.mode, "RGBA")

            # Test with custom badge
            img_badge = video_template.create_headline_banner_image(
                headline_badge="TIN ĐẶC BIỆT",
                headline_title="Thủ tướng yêu cầu rà soát chương trình phổ cập",
                width=1080,
                height=1920,
                position=pos,
            )
            self.assertEqual(img_badge.size, (1080, 1920))
            self.assertEqual(img_badge.mode, "RGBA")

    def test_create_headline_banner_scale_color_and_long_title(self):
        # Test long Vietnamese headline that would previously be truncated
        long_headline = "Bộ Giáo dục và Đào tạo chính thức công bố phương án tổ chức kỳ thi tốt nghiệp trung học phổ thông từ năm 2025"
        for scale in [0.7, 1.0, 1.4]:
            for color in ["#FFFFFF", "#FACC15", "#EF4444", "#38BDF8"]:
                img = video_template.create_headline_banner_image(
                    headline_badge="BẢN TIN",
                    headline_title=long_headline,
                    width=1080,
                    height=1920,
                    position="top",
                    pos_x=50.0,
                    pos_y=8.0,
                    font_scale=scale,
                    text_color=color,
                )
                self.assertEqual(img.size, (1080, 1920))
                self.assertEqual(img.mode, "RGBA")

    def test_create_source_badge_scale_and_color(self):
        for scale in [0.6, 1.0, 1.5]:
            for color in ["#F8FAFC", "#FACC15", "#38BDF8"]:
                img = video_template.create_source_badge_image(
                    text="Nguồn: Báo Tuổi Trẻ Online",
                    width=1080,
                    height=1920,
                    position="top_right",
                    pos_x=75.0,
                    pos_y=12.0,
                    font_scale=scale,
                    text_color=color,
                )
                self.assertEqual(img.size, (1080, 1920))
                self.assertEqual(img.mode, "RGBA")

    def test_create_logo_overlay_image(self):
        import tempfile
        dummy_logo = Image.new("RGBA", (200, 100), (0, 120, 255, 255))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            logo_path = tf.name
            dummy_logo.save(logo_path)

        try:
            for pos in ["top_left", "top_right", "bottom_left", "bottom_right"]:
                img = video_template.create_logo_overlay_image(
                    logo_path=logo_path,
                    width=1080,
                    height=1920,
                    position=pos,
                    logo_width=140,
                )
                self.assertEqual(img.size, (1080, 1920))
                self.assertEqual(img.mode, "RGBA")
        finally:
            if os.path.exists(logo_path):
                os.remove(logo_path)

    def test_save_uploaded_logo(self):
        import io
        dummy_logo = Image.new("RGBA", (64, 64), (255, 255, 0, 255))
        buf = io.BytesIO()
        dummy_logo.save(buf, format="PNG")
        saved = video_template.save_uploaded_logo(buf.getvalue(), "test_logo.png")
        self.assertTrue(os.path.exists(saved))
        self.assertTrue(saved.endswith("test_logo.png"))


if __name__ == "__main__":
    unittest.main()
