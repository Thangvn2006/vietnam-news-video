# 🎬 VietNamNewsVideo - Automated AI News Video Generator

<div align="center">

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](https://github.com/Thangvn2006/vietnam-news-video)
[![GitHub Repository](https://img.shields.io/badge/GitHub-Thangvn2006%2Fvietnam--news--video-181717.svg?logo=github)](https://github.com/Thangvn2006/vietnam-news-video)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**An open-source solution that completely automates news and short video production from any article link or topic.**

[Installation Guide](#-installation-guide) • [Key Features](#-key-features) • [Free API Setup](#-free-api-keys-setup) • [Auto-Update](#-1-click-auto-update) • [Vietnamese README](README.md)

</div>

---

## 🌟 Introduction

**VietNamNewsVideo** is an AI-powered system designed to generate professional news broadcasts, reels, and reportage videos in **9:16 (TikTok, Shorts, Reels)** or **16:9 (YouTube)** within minutes.

By providing a **news article URL** (e.g. VnExpress, Tuổi Trẻ, Dân Trí...) or typing any **subject / topic**, the system automatically:
1. 📰 **Extracts & Summarizes:** Scrapes the article, extracts the source citation, and drafts an engaging broadcast script.
2. 🔍 **Finds HD Footage:** Automatically queries and matches high-resolution HD/4K stock footage and photos from Pexels for every sentence.
3. 🎙️ **Natural Voiceover:** Uses natural Vietnamese AI voiceover via EdgeTTS (free, no keys required) or ElevenLabs.
4. 📝 **Generates Subtitles:** Synchronizes dynamic subtitles timed perfectly to speech audio.
5. 🎨 **Interactive Draggable Graphic Overlays:** Freely positions headline banners, source badges, logos, and television news frames directly on the simulated canvas.

---

## ✨ Key Features

* **📰 Smart News Scraping**: Automatically extracts titles, content, and sources from major news sites.
* **🖱️ Interactive Draggable Canvas**:
  * Drag and position **Headline Banner**, **Source Badge**, and **Channel Logo** anywhere on the screen.
  * Configure individual display durations (e.g., headline only for the first 5 seconds, logo permanent).
* **🖼️ Television News Frames**: Supports transparent checkerboard news borders or custom PNG templates designed with Photoshop/Canva.
* **🗣️ 100% Free Vietnamese Text-to-Speech**: Built-in Microsoft EdgeTTS (`vi-VN-HoaiMyNeural`, `vi-VN-NamMinhNeural`) without requiring registration or API keys.
* **📁 Video Gallery Management**: Download, preview, and cleanly delete generated videos from disk.
* **⚡ System Diagnostic & 1-Click Auto-Update**:
  * Automatically checks Python, FFmpeg, Git, storage permissions, and API status.
  * Checks for upstream GitHub commits and updates code with 1 click.

---

## 💻 System Requirements

* **Operating System**: Windows 10/11 (64-bit), macOS, or Linux.
* **Python**: **Python 3.10, 3.11, or 3.12** (Python 3.10 or 3.11 recommended).
  * ⚠️ *Important:* Ensure you check **`Add python.exe to PATH`** during installation.
* **FFmpeg**: Automatically detected from system PATH or bundled fallback.
* **Git**: Required for cloning repository and 1-click auto-updates.

---

## 🚀 Installation Guide

### Method 1: Automated 1-Click Setup on Windows (Recommended)

1. **Clone or download repository:**
   ```bash
   git clone https://github.com/Thangvn2006/vietnam-news-video.git
   cd vietnam-news-video
   ```

2. **Run automated installer:**
   - Double-click **`cai_dat.bat`**.
   - It will automatically create the `venv` virtual environment, install dependencies from `requirements.txt`, and initialize `config.toml`.

3. **Launch the application:**
   - Double-click **`khoi_dong.bat`**.
   - Your browser will automatically open: `http://localhost:8501`.

---

### Method 2: Manual Installation via Terminal

For Linux, macOS, or advanced terminal setup:

```bash
# 1. Clone repository
git clone https://github.com/Thangvn2006/vietnam-news-video.git
cd vietnam-news-video

# 2. Create virtual environment
python -m venv venv

# 3. Activate virtual environment
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

# 4. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 5. Initialize configuration
cp config.example.toml config.toml

# 6. Launch WebUI
streamlit run webui/Main.py
```

---

## 🔑 Free API Keys Setup

### 1. Google Gemini API (Scripting)
* **Cost:** Free tier available.
* **How to get:**
  1. Visit [Google AI Studio](https://aistudio.google.com/).
  2. Sign in with your Google account.
  3. Click **Get API Key** &rarr; **Create API key**.
  4. Paste into the **AI API Settings** tab in WebUI.

### 2. Pexels API (Stock Footage & Images)
* **Cost:** 100% Free.
* **How to get:**
  1. Visit [Pexels API Documentation](https://www.pexels.com/api/).
  2. Register a free account.
  3. Copy your API Key and paste it into the **AI API Settings** tab in WebUI.

---

## ⚡ 1-Click Auto-Update

To update to the latest version:
1. Open WebUI and go to the **`⚡ System & Update`** tab.
2. Click **`🔍 Check for Updates Now`**.
3. If new updates are found on GitHub, click **`🚀 Upgrade Now (1-Click)`** to synchronize the codebase and reload.

---

## 🤝 Contributing & Support

* **Repository:** [https://github.com/Thangvn2006/vietnam-news-video](https://github.com/Thangvn2006/vietnam-news-video)
* **Issues & Bug Reports:** [GitHub Issues](https://github.com/Thangvn2006/vietnam-news-video/issues)
* Star ⭐️ this project on GitHub if you find it helpful!
