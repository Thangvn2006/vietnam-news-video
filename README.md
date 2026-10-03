# 🎬 VietNamNewsVideo - Hệ Thống Tạo Video Tin Tức Tự Động Bằng AI

<div align="center">

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](https://github.com/Thangvn2006/vietnam-news-video)
[![GitHub Repository](https://img.shields.io/badge/GitHub-Thangvn2006%2Fvietnam--news--video-181717.svg?logo=github)](https://github.com/Thangvn2006/vietnam-news-video)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**Giải pháp mã nguồn mở hàng đầu giúp tự động hóa 100% quy trình sản xuất video tin tức, phóng sự ngắn và thời sự từ đường link bài báo hoặc chủ đề bất kỳ.**

[Hướng Dẫn Cài Đặt](#-hướng-dẫn-cài-đặt-chi-tiết) • [Tính Năng](#-tính-năng-nổi-bật) • [Cấu Hình API](#-hướng-dẫn-lấy-api-key-miễn-phí) • [Cập Nhật Tự Động](#-tự-động-cập-nhật) • [English README](README-en.md)

</div>

---

## 🌟 Giới Thiệu Công Cụ

**VietNamNewsVideo** là ứng dụng chuyên nghiệp ứng dụng Trí tuệ Nhân tạo (AI) để sản xuất video thời sự, tin tức ngắn chuẩn định dạng **9:16 (TikTok, Shorts, Reels)** hoặc **16:9 (YouTube, TV)** chỉ trong vài phút.

Chỉ cần nhập một **đường dẫn bài báo** (VnExpress, Tuổi Trẻ, Dân Trí...) hoặc gõ một **chủ đề**, hệ thống sẽ tự động:
1. 📰 **Trích xuất & Tóm tắt:** Đọc bài báo, trích dẫn nguồn tin và viết kịch bản video cô đọng, cuốn hút.
2. 🔍 **Tìm kiếm tư liệu:** Tự động tìm kiếm các cảnh quay (footage) và hình ảnh độ phân giải cao (HD/4K) phù hợp nhất với từng câu thoại từ kho tư liệu Pexels miễn phí.
3. 🎙️ **Thuyết minh lồng tiếng:** Sử dụng giọng đọc AI tự nhiên chuẩn tiếng Việt (EdgeTTS hoặc ElevenLabs).
4. 📝 **Tạo phụ đề sinh động:** Tự động tạo phụ đề chạy từng câu, canh chuẩn từng giây khớp với giọng đọc.
5. 🎨 **Đồ họa bản tin kéo thả trực quan:** Tùy biến thanh tiêu đề, nhãn nguồn tin tức, logo kênh và khung viền thời sự trực tiếp trên màn hình xem trước.

---

## ✨ Tính Năng Nổi Bật

* **📰 Cào báo tự động thông minh**: Hỗ trợ bóc tách nội dung từ hầu hết các trang báo điện tử lớn tại Việt Nam (VnExpress, Tuổi Trẻ, Dân Trí, Thanh Niên, VietnamNet...).
* **🖱️ Khung xem trước kéo thả trực quan (Interactive Canvas)**:
  * Nhấn giữ và kéo thả tự do vị trí **Tiêu đề**, **Nhãn nguồn**, **Logo thương hiệu** trên video.
  * Tùy chỉnh thời lượng xuất hiện riêng biệt cho từng thành phần (ví dụ: tiêu đề hiện 5s đầu, logo hiện vĩnh viễn).
* **🖼️ Khung viền tin tức truyền hình (News Frame)**: Hỗ trợ khung viền thời sự cờ caro trong suốt, hoặc tải khung đồ họa tự thiết kế từ Canva/Photoshop lên.
* **🗣️ Giọng đọc tiếng Việt miễn phí 100%**: Tích hợp sẵn Microsoft EdgeTTS với giọng đọc mượt mà (`vi-VN-HoaiMyNeural`, `vi-VN-NamMinhNeural`) không cần tài khoản hay API key.
* **📁 Quản lý video tiện lợi**: Thư viện video đã tạo lưu trữ tại `storage/final_videos/`, hỗ trợ xem trực tiếp, tải về máy tính và xóa an toàn (từng video hoặc xóa tất cả).
* **⚡ Tự kiểm tra hệ thống & Tự động cập nhật 1-Click**:
  * Tự động kiểm tra môi trường: Python, FFmpeg, Git, quyền ổ đĩa và trạng thái API.
  * Tự động phát hiện phiên bản mới trên GitHub và nâng cấp mã nguồn an toàn chỉ bằng 1 nút bấm.

---

## 💻 Yêu Cầu Hệ Thống

* **Hệ điều hành**: Windows 10/11 (64-bit), macOS hoặc Linux.
* **Python**: Phiên bản **Python 3.10, 3.11 hoặc 3.12** (khuyên dùng Python 3.10 hoặc 3.11).
  * ⚠️ *Lưu ý quan trọng:* Khi cài đặt Python trên Windows, bắt buộc phải tích chọn ô **`Add python.exe to PATH`**.
* **FFmpeg**: Công cụ xử lý video/âm thanh (hệ thống sẽ tự nhận diện nếu đã có, hoặc tự động dùng thư viện đi kèm).
* **Git**: Dùng để tải mã nguồn và tự động cập nhật phiên bản mới.

---

## 🚀 Hướng Dẫn Cài Đặt Chi Tiết

### Cách 1: Cài đặt tự động 1-Click trên Windows (Khuyên dùng)

1. **Tải mã nguồn về máy tính:**
   Mở cửa sổ `Command Prompt` (cmd) hoặc `PowerShell` và chạy:
   ```bash
   git clone https://github.com/Thangvn2006/vietnam-news-video.git
   cd vietnam-news-video
   ```
   *(Hoặc bấm nút xanh **Code** &rarr; **Download ZIP** trên GitHub rồi giải nén thư mục).*

2. **Cài đặt môi trường tự động:**
   - Nhấp đúp chuột vào tệp **`cai_dat.bat`**.
   - Tệp sẽ tự động tạo môi trường ảo `venv`, cài đặt đầy đủ tất cả các gói thư viện cần thiết từ `requirements.txt` và khởi tạo tệp cấu hình `config.toml`.
   - Quá trình này chỉ cần chạy **1 lần duy nhất** lúc mới tải dự án về máy.

3. **Khởi động ứng dụng:**
   - Nhấp đúp chuột vào tệp **`khoi_dong.bat`**.
   - Cửa sổ ứng dụng WebUI sẽ tự động được mở trên trình duyệt của bạn tại địa chỉ: `http://localhost:8501`.

---

### Cách 2: Cài đặt thủ công bằng dòng lệnh (Dành cho mọi hệ điều hành)

Nếu bạn sử dụng Linux, macOS hoặc muốn quản lý cài đặt thủ công qua dòng lệnh:

```bash
# 1. Tải repository
git clone https://github.com/Thangvn2006/vietnam-news-video.git
cd vietnam-news-video

# 2. Khởi tạo môi trường ảo Python
python -m venv venv

# 3. Kích hoạt môi trường ảo
# Trên Windows:
venv\Scripts\activate
# Trên macOS / Linux:
source venv/bin/activate

# 4. Nâng cấp pip và cài đặt thư viện phụ thuộc
pip install --upgrade pip
pip install -r requirements.txt

# 5. Khởi tạo tệp cấu hình config.toml
cp config.example.toml config.toml   # Trên Linux/macOS
# copy config.example.toml config.toml # Trên Windows

# 6. Khởi động WebUI
streamlit run webui/Main.py
```

---

## 🔑 Hướng Dẫn Lấy API Key Miễn Phí

Để tạo video, bạn chỉ cần chuẩn bị 2 khóa API miễn phí sau:

### 1. Google Gemini API (Tạo kịch bản AI)
* **Chi phí:** Hoàn toàn **Miễn phí** với hạn mức lớn.
* **Cách lấy:**
  1. Truy cập [Google AI Studio](https://aistudio.google.com/).
  2. Đăng nhập bằng tài khoản Google (Gmail).
  3. Chọn **Get API Key** &rarr; **Create API key in new project**.
  4. Sao chép chuỗi khóa và dán vào tab **`🔑 Nhập API AI`** trên WebUI.

### 2. Pexels API (Kho video & hình ảnh minh họa)
* **Chi phí:** Hoàn toàn **Miễn phí 100%**.
* **Cách lấy:**
  1. Truy cập [Pexels API Documentation](https://www.pexels.com/api/).
  2. Bấm **Get Started** để đăng ký tài khoản miễn phí.
  3. Vào phần **Your API Key** và sao chép khóa API.
  4. Dán vào mục **Pexels API Key** trong tab **`🔑 Nhập API AI`** trên WebUI và bấm **Lưu cấu hình**.

---

## 📖 Hướng Dẫn Sử Dụng Nhanh

1. Mở WebUI tại `http://localhost:8501`.
2. Chuyển sang tab **`🔑 Nhập API AI`**, điền API Key Gemini và Pexels rồi bấm lưu.
3. Chuyển sang tab **`🎬 Tạo video`**:
   - Chọn **Từ link bài báo** (dán link báo điện tử) hoặc **Từ chủ đề** (nhập chủ đề bạn muốn).
   - Chọn tỷ lệ khung hình: **9:16 (Dọc)** cho video ngắn hoặc **16:9 (Ngang)** cho video dài.
   - Chọn giọng đọc tiếng Việt mong muốn (HoaiMy hoặc NamMinh).
   - Xem trước tại khung canvas và dùng chuột kéo thả tiêu đề, nguồn tin, logo đến vị trí ưng ý.
4. Bấm nút đỏ **🚀 Bắt đầu tạo video**.
5. Theo dõi tiến trình tự động và xem/tải video tại tab **`📁 Video đã tạo`**.

---

## ⚡ Tự Động Cập Nhật (Auto-Update)

Bạn không cần phải tải lại mã nguồn thủ công khi có phiên bản mới:
1. Mở WebUI và chuyển sang tab **`⚡ Tự kiểm tra & Cập nhật`**.
2. Nhấn nút **`🔍 Kiểm tra bản cập nhật ngay`**.
3. Nếu phát hiện có commit mới trên GitHub, hãy bấm **`🚀 Cập nhật ngay (1-Click Update)`**. Hệ thống sẽ tự động kéo các thay đổi mới nhất về máy và cho phép bạn tải lại ứng dụng ngay lập tức!

---

## 📂 Cấu Trúc Dự Án

```
vietnam-news-video/
├── app/
│   ├── config/             # Quản lý cấu hình config.toml
│   ├── controllers/        # REST API endpoints
│   ├── models/             # Schema dữ liệu Pydantic
│   ├── services/           # Lõi xử lý kịch bản, video, scraper, updater
│   │   ├── article_scraper.py  # Cào tin tức báo chí
│   │   ├── video_template.py   # Render đồ họa, tiêu đề, logo, khung viền
│   │   ├── system_updater.py   # Kiểm tra hệ thống & tự cập nhật
│   │   └── video.py            # Biên tập và xuất file video
│   └── utils/              # Tiện ích hệ thống, FFmpeg, logging
├── webui/
│   ├── Main.py             # Giao diện WebUI chính (Streamlit)
│   ├── components/         # Canvas kéo thả đồ họa tương tác (HTML5/JS)
│   └── styles.css          # Giao diện Dark mode hiện đại
├── storage/
│   ├── final_videos/       # Thư mục lưu các video thành phẩm đã tạo
│   └── tasks/              # Dữ liệu phân cảnh và tác vụ tạm thời
├── cai_dat.bat             # Tự động cài đặt môi trường 1-click (Windows)
├── khoi_dong.bat           # Khởi động WebUI 1-click (Windows)
├── requirements.txt        # Danh sách thư viện Python
└── README.md               # Tài liệu hướng dẫn sử dụng
```

---

## 🤝 Đóng Góp & Hỗ Trợ

* **Repository:** [https://github.com/Thangvn2006/vietnam-news-video](https://github.com/Thangvn2006/vietnam-news-video)
* **Báo cáo lỗi & Góp ý tính năng:** [GitHub Issues](https://github.com/Thangvn2006/vietnam-news-video/issues)
* Nếu thấy công cụ hữu ích, đừng quên tặng dự án **1 Star ⭐️** trên GitHub nhé!

---

<div align="center">
  Phát triển với ❤️ dành cho cộng đồng sáng tạo nội dung tin tức Việt Nam.
</div>
