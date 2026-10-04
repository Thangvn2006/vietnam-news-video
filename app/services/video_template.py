import os
from pathlib import Path
from typing import Optional
from PIL import Image, ImageDraw, ImageFont
from loguru import logger

from app.utils import utils

TEMPLATES_DIR_NAME = "templates"


def get_templates_dir() -> str:
    """Return the absolute path to storage/templates directory, creating it if needed."""
    storage_dir = utils.storage_dir(TEMPLATES_DIR_NAME, create=True)
    return storage_dir


def get_default_font(size: int = 24) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load BeVietnamPro font if available, fallback to STHeiti or PIL default."""
    try:
        font_dir = utils.font_dir()
        preferred_fonts = [
            "BeVietnamPro-Bold.ttf",
            "BeVietnamPro-Medium.ttf",
            "STHeitiMedium.ttc",
            "MicrosoftYaHeiBold.ttc",
        ]
        for font_name in preferred_fonts:
            full_path = os.path.join(font_dir, font_name)
            if os.path.exists(full_path):
                return ImageFont.truetype(full_path, size)
    except Exception as exc:
        logger.debug(f"Failed to load custom font: {exc}")

    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def create_frame_template(
    width: int,
    height: int,
    with_checkerboard: bool = False,
    title: str = "",
    source_text: str = "",
) -> Image.Image:
    """
    Generate a professional news broadcast frame template.
    If with_checkerboard is True: The central viewport has a checkerboard pattern
    indicating the transparent video area, with guide text for Photoshop/Canva users.
    If with_checkerboard is False: The central viewport is 100% transparent RGBA.
    """
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad_top = int(height * 0.08)
    pad_bottom = int(height * 0.12)
    pad_x = 30

    font_large = get_default_font(size=int(pad_top * 0.42))
    font_small = get_default_font(size=int(pad_top * 0.32))

    if with_checkerboard:
        # Draw checkerboard pattern in central viewport
        cell = 48 if width > 1200 else 36
        for y in range(pad_top, height - pad_bottom, cell):
            for x in range(pad_x, width - pad_x, cell):
                color = (
                    (255, 255, 255, 255)
                    if ((x // cell) + (y // cell)) % 2 == 0
                    else (226, 232, 240, 255)
                )
                draw.rectangle([x, y, x + cell, y + cell], fill=color)

        # Center watermark banner
        box_w = min(int(width * 0.8), 850)
        box_h = 110
        bx0 = (width - box_w) // 2
        by0 = (height - box_h) // 2
        draw.rounded_rectangle(
            [bx0, by0, bx0 + box_w, by0 + box_h],
            radius=16,
            fill=(15, 23, 42, 225),
            outline=(255, 255, 255, 120),
            width=2,
        )
        txt1 = "VÙNG HIỂN THỊ HÌNH ẢNH / VIDEO"
        txt2 = "(Vùng cờ caro trong suốt - Tải về chỉnh sửa bằng Photoshop/Canva)"
        draw.text(
            (width // 2, by0 + 32),
            txt1,
            font=font_small,
            fill=(255, 255, 255, 255),
            anchor="mm",
        )
        draw.text(
            (width // 2, by0 + 76),
            txt2,
            font=font_small,
            fill=(203, 213, 225, 255),
            anchor="mm",
        )

    # Top broadcast header bar
    draw.rectangle([0, 0, width, pad_top], fill=(15, 23, 42, 245))
    draw.rectangle([0, pad_top - 4, width, pad_top], fill=(225, 29, 72, 255))

    # Breaking news badge (optional)
    if title:
        badge_w = min(int(width * 0.28), 260)
        badge_h = int(pad_top * 0.55)
        badge_y0 = (pad_top - badge_h) // 2
        draw.rounded_rectangle(
            [pad_x, badge_y0, pad_x + badge_w, badge_y0 + badge_h],
            radius=8,
            fill=(225, 29, 72, 255),
        )
        draw.text(
            (pad_x + badge_w // 2, badge_y0 + badge_h // 2),
            title,
            font=font_small,
            fill=(255, 255, 255, 255),
            anchor="mm",
        )

    # Corner source badge (top-right)
    if source_text:
        src_label = f"📌 {source_text}"
        src_w = min(int(width * 0.36), 340)
        src_h = int(pad_top * 0.55)
        src_x0 = width - pad_x - src_w
        src_y0 = (pad_top - src_h) // 2
        draw.rounded_rectangle(
            [src_x0, src_y0, src_x0 + src_w, src_y0 + src_h],
            radius=8,
            fill=(30, 41, 59, 235),
            outline=(255, 255, 255, 90),
            width=1,
        )
        draw.text(
            (src_x0 + src_w // 2, src_y0 + src_h // 2),
            src_label,
            font=font_small,
            fill=(241, 245, 249, 255),
            anchor="mm",
        )

    # Bottom ticker bar (for subtitles)
    draw.rectangle([0, height - pad_bottom, width, height], fill=(15, 23, 42, 245))
    draw.rectangle(
        [0, height - pad_bottom, width, height - pad_bottom + 4],
        fill=(37, 99, 235, 255),
    )

    # Corner brackets [ ]
    bracket_len = 45
    bracket_w = 3
    col = (255, 255, 255, 160)
    # top-left
    draw.line([(pad_x, pad_top + 8), (pad_x, pad_top + 8 + bracket_len)], fill=col, width=bracket_w)
    draw.line([(pad_x, pad_top + 8), (pad_x + bracket_len, pad_top + 8)], fill=col, width=bracket_w)
    # top-right
    draw.line([(width - pad_x, pad_top + 8), (width - pad_x, pad_top + 8 + bracket_len)], fill=col, width=bracket_w)
    draw.line([(width - pad_x, pad_top + 8), (width - pad_x - bracket_len, pad_top + 8)], fill=col, width=bracket_w)
    # bottom-left
    draw.line([(pad_x, height - pad_bottom - 8), (pad_x, height - pad_bottom - 8 - bracket_len)], fill=col, width=bracket_w)
    draw.line([(pad_x, height - pad_bottom - 8), (pad_x + bracket_len, height - pad_bottom - 8)], fill=col, width=bracket_w)
    # bottom-right
    draw.line([(width - pad_x, height - pad_bottom - 8), (width - pad_x, height - pad_bottom - 8 - bracket_len)], fill=col, width=bracket_w)
    draw.line([(width - pad_x, height - pad_bottom - 8), (width - pad_x - bracket_len, height - pad_bottom - 8)], fill=col, width=bracket_w)

    return img


def _hex_to_rgba(color_str: str, alpha: int = 255, default_rgba=(255, 255, 255, 255)) -> tuple[int, int, int, int]:
    """Parse hex color string (e.g. #FFF, #FFFFFF, #RRGGBBAA) to RGBA tuple."""
    if not color_str:
        return default_rgba
    c = str(color_str).strip().lstrip("#")
    if len(c) == 3:
        c = "".join([x * 2 for x in c])
    if len(c) == 6:
        try:
            return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha)
        except ValueError:
            return default_rgba
    elif len(c) == 8:
        try:
            return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), int(c[6:8], 16))
        except ValueError:
            return default_rgba
    return default_rgba


def create_source_badge_image(
    text: str,
    width: int = 1080,
    height: int = 1920,
    position: str = "top_right",
    pos_x: Optional[float] = None,
    pos_y: Optional[float] = None,
    font_scale: float = 1.0,
    text_color: str = "#F8FAFC",
) -> Image.Image:
    """
    Generate an RGBA image of the video resolution containing only the source badge
    at coordinates (pos_x%, pos_y%) or designated corner, with customizable font scale and text color.
    """
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if not text:
        return img

    draw = ImageDraw.Draw(img)
    label = text if text.startswith("📌") else f"📌 {text}"
    scale = max(0.5, min(2.5, float(font_scale or 1.0)))
    font_size = max(13, int(height * 0.015 * scale))
    font = get_default_font(size=font_size)

    bbox = font.getbbox(label)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    pad_h = max(6, int(10 * scale))
    pad_w = max(10, int(18 * scale))
    badge_w = text_w + pad_w * 2
    badge_h = text_h + pad_h * 2

    margin_x = int(36 * scale)
    margin_y = int(60 * scale)
    bottom_offset = int(140 * scale)

    if pos_x is not None and pos_y is not None:
        px = (pos_x / 100.0) * width
        py = (pos_y / 100.0) * height
        x0 = max(10, min(width - badge_w - 10, int(px)))
        y0 = max(10, min(height - badge_h - 10, int(py)))
    elif position == "top_left":
        x0, y0 = margin_x, margin_y
    elif position == "top_center":
        x0, y0 = (width - badge_w) // 2, margin_y
    elif position == "bottom_left":
        x0, y0 = margin_x, height - badge_h - bottom_offset
    elif position == "bottom_center":
        x0, y0 = (width - badge_w) // 2, height - badge_h - bottom_offset
    elif position == "bottom_right":
        x0, y0 = width - badge_w - margin_x, height - badge_h - bottom_offset
    else:  # top_right
        x0, y0 = width - badge_w - margin_x, margin_y

    x1, y1 = x0 + badge_w, y0 + badge_h
    corner_radius = max(6, int(10 * scale))
    draw.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=corner_radius,
        fill=(15, 23, 42, 235),
        outline=(255, 255, 255, 90),
        width=1,
    )
    col = _hex_to_rgba(text_color, alpha=255, default_rgba=(248, 250, 252, 255))
    draw.text((x0 + pad_w, y0 + pad_h - 2), label, font=font, fill=col)
    return img


def create_headline_banner_image(
    headline_badge: str,
    headline_title: str,
    width: int,
    height: int,
    position: str = "top",
    pos_x: Optional[float] = None,
    pos_y: Optional[float] = None,
    font_scale: float = 1.0,
    text_color: str = "#FFFFFF",
) -> Image.Image:
    """
    Generate an RGBA image of the video resolution containing the news headline banner
    (e.g. [BẢN TIN NÓNG] + Title) positioned at coordinates (pos_x%, pos_y%) or top, center, bottom.
    Supports font scaling, customizable text color, and smart multi-line wrapping so long
    headlines can display completely on screen without truncation.
    """
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if not headline_title:
        return img

    draw = ImageDraw.Draw(img)
    badge_text = (headline_badge or "").strip().upper()
    title_text = (headline_title or "").strip()

    scale = max(0.5, min(2.5, float(font_scale or 1.0)))
    base_font_size = max(15, int(height * 0.018 * scale))

    pad_x = max(12, int(18 * scale))
    pad_y = max(8, int(10 * scale))
    gap = int(14 * scale) if badge_text else 0

    if badge_text:
        badge_font = get_default_font(size=max(13, int(height * 0.015 * scale)))
        badge_bbox = badge_font.getbbox(badge_text)
        badge_w = (badge_bbox[2] - badge_bbox[0]) + int(24 * scale)
        badge_h = (badge_bbox[3] - badge_bbox[1]) + int(14 * scale)
    else:
        badge_font = None
        badge_w = 0
        badge_h = 0

    max_banner_w = int(width * 0.94)
    avail_text_w = max_banner_w - (badge_w + gap if badge_text else 0) - pad_x * 2

    # Smart text wrap and auto-fitting:
    # If headline is long, wrap into multiple lines cleanly without truncating
    title_font = get_default_font(size=base_font_size)

    def wrap_text_to_lines(text: str, font, max_w: int) -> list[str]:
        words = text.split()
        if not words:
            return []
        res = []
        cur = []
        for w in words:
            test = " ".join(cur + [w])
            bbox = font.getbbox(test)
            if (bbox[2] - bbox[0]) <= max_w or not cur:
                cur.append(w)
            else:
                res.append(" ".join(cur))
                cur = [w]
        if cur:
            res.append(" ".join(cur))
        return res

    lines = wrap_text_to_lines(title_text, title_font, avail_text_w)
    cur_size = base_font_size
    while len(lines) > 2 and cur_size > max(14, int(base_font_size * 0.7)):
        cur_size -= 2
        title_font = get_default_font(size=cur_size)
        lines = wrap_text_to_lines(title_text, title_font, avail_text_w)

    sample_bbox = title_font.getbbox("AgMột")
    line_h = max(18, sample_bbox[3] - sample_bbox[1])
    line_spacing = max(4, int(line_h * 0.22))
    total_text_h = len(lines) * line_h + max(0, len(lines) - 1) * line_spacing

    line_widths = [title_font.getbbox(line)[2] - title_font.getbbox(line)[0] for line in lines]
    max_lw = max(line_widths) if line_widths else 0

    total_content_w = (badge_w + gap if badge_text else 0) + max_lw
    banner_w = min(max_banner_w, total_content_w + pad_x * 2)
    banner_h = max(badge_h, total_text_h) + pad_y * 2

    # Calculate position
    if pos_x is not None and pos_y is not None:
        px = (pos_x / 100.0) * width
        py = (pos_y / 100.0) * height
        x0 = max(10, min(width - banner_w - 10, int(px - banner_w / 2)))
        y0 = max(10, min(height - banner_h - 10, int(py)))
    else:
        margin_x = (width - banner_w) // 2
        x0 = margin_x
        if position == "center":
            y0 = (height - banner_h) // 2
        elif position == "bottom":
            y0 = height - banner_h - int(height * 0.14)
        else:  # top
            y0 = int(height * 0.02) + 20

    y1 = y0 + banner_h
    x1 = x0 + banner_w

    # Sleek dark frosted glass background
    corner_radius = max(8, int(12 * scale))
    draw.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=corner_radius,
        fill=(10, 15, 29, 230),
        outline=(255, 255, 255, 55),
        width=1,
    )

    # Optional headline badge
    if badge_text and badge_font:
        bx0 = x0 + pad_x
        by0 = y0 + (banner_h - badge_h) // 2
        bx1 = bx0 + badge_w
        by1 = by0 + badge_h
        draw.rounded_rectangle(
            [bx0, by0, bx1, by1],
            radius=max(6, int(8 * scale)),
            fill=(225, 29, 72, 255),
        )
        draw.text((bx0 + int(12 * scale), by0 + int(6 * scale)), badge_text, font=badge_font, fill=(255, 255, 255, 255))
        tx = bx1 + gap
    else:
        tx = x0 + pad_x

    # Render headline title lines with custom text color
    hl_color = _hex_to_rgba(text_color, alpha=255, default_rgba=(255, 255, 255, 255))
    text_start_y = y0 + (banner_h - total_text_h) // 2 - 1
    for i, line in enumerate(lines):
        curr_y = text_start_y + i * (line_h + line_spacing)
        draw.text((tx, curr_y), line, font=title_font, fill=hl_color)

    return img


def create_logo_overlay_image(
    logo_path: str,
    width: int,
    height: int,
    position: str = "top_left",
    logo_width: int = 140,
    pos_x: Optional[float] = None,
    pos_y: Optional[float] = None,
) -> Image.Image:
    """
    Generate an RGBA image of the video resolution containing the brand logo
    at coordinates (pos_x%, pos_y%) or designated corner.
    """
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if not logo_path or not os.path.exists(logo_path):
        return img

    try:
        with Image.open(logo_path) as logo_src:
            logo = logo_src.convert("RGBA")
            lw = max(40, min(logo_width, int(width * 0.45)))
            aspect = logo.height / max(logo.width, 1)
            lh = max(20, int(lw * aspect))
            logo_resized = logo.resize((lw, lh), Image.Resampling.LANCZOS)

            margin_x = 36
            margin_y = 48
            bottom_offset = 140

            if pos_x is not None and pos_y is not None:
                x = max(10, min(width - lw - 10, int((pos_x / 100.0) * width)))
                y = max(10, min(height - lh - 10, int((pos_y / 100.0) * height)))
            elif position == "top_right":
                x = width - lw - margin_x
                y = margin_y
            elif position == "bottom_left":
                x = margin_x
                y = height - lh - bottom_offset
            elif position == "bottom_right":
                x = width - lw - margin_x
                y = height - lh - bottom_offset
            else:  # top_left
                x = margin_x
                y = margin_y

            img.paste(logo_resized, (x, y), logo_resized)
    except Exception as exc:
        logger.warning(f"Failed to create logo overlay: {exc}")

    return img


def create_frame_overlay_image(
    frame_path: str,
    width: int,
    height: int,
    pos_x: Optional[float] = 0.0,
    pos_y: Optional[float] = 0.0,
) -> Image.Image:
    """
    Generate an RGBA image containing custom frame or overlay image
    at coordinates (pos_x%, pos_y%) or full canvas.
    """
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if not frame_path or not os.path.exists(frame_path):
        return img

    try:
        with Image.open(frame_path) as frame_src:
            frame = frame_src.convert("RGBA")
            if (pos_x is None or pos_x == 0.0) and (pos_y is None or pos_y == 0.0):
                frame_resized = frame.resize((width, height), Image.Resampling.LANCZOS)
                img.paste(frame_resized, (0, 0), frame_resized)
            else:
                fx = max(0, min(width - 20, int(((pos_x or 0.0) / 100.0) * width)))
                fy = max(0, min(height - 20, int(((pos_y or 0.0) / 100.0) * height)))
                fw = min(width, frame.width)
                fh = min(height, frame.height)
                frame_resized = frame.resize((fw, fh), Image.Resampling.LANCZOS)
                img.paste(frame_resized, (fx, fy), frame_resized)
    except Exception as exc:
        logger.warning(f"Failed to create frame overlay image: {exc}")

    return img


def save_uploaded_logo(file_bytes: bytes, filename: str) -> str:
    """Save an uploaded brand logo image to storage/logos/ and return the file path."""
    logos_dir = os.path.join(utils.storage_dir(), "logos")
    os.makedirs(logos_dir, exist_ok=True)
    clean_name = os.path.basename(filename)
    dest_path = os.path.join(logos_dir, clean_name)
    with open(dest_path, "wb") as f:
        f.write(file_bytes)
    return dest_path


def ensure_default_templates_exist() -> dict[str, str]:
    """Ensure standard templates (both 9:16 and 16:9, checkerboard and transparent) exist on disk."""
    templates_dir = get_templates_dir()
    templates = {
        "template_9_16_checkerboard.png": (1080, 1920, True),
        "template_9_16_transparent.png": (1080, 1920, False),
        "template_16_9_checkerboard.png": (1920, 1080, True),
        "template_16_9_transparent.png": (1920, 1080, False),
    }

    result = {}
    for filename, (w, h, with_caro) in templates.items():
        file_path = os.path.join(templates_dir, filename)
        if not os.path.exists(file_path):
            img = create_frame_template(
                width=w,
                height=h,
                with_checkerboard=with_caro,
                title="",
                source_text="Nguồn: Báo chí",
            )
            img.save(file_path, "PNG")
            logger.info(f"Generated default template: {file_path}")
        result[filename] = file_path
    return result


def get_available_templates(aspect: str = "9:16") -> list[dict[str, str]]:
    """Return list of available templates matching current aspect ratio."""
    ensure_default_templates_exist()
    templates_dir = get_templates_dir()
    items = []

    is_portrait = "9:16" in aspect or "portrait" in str(aspect).lower()

    if is_portrait:
        items.append({
            "id": "none",
            "name": "Không dùng khung viền",
            "path": "",
            "is_checkerboard": False,
        })
        items.append({
            "id": "template_9_16_transparent.png",
            "name": "Khung bản tin thời sự (9:16 Dọc)",
            "path": os.path.join(templates_dir, "template_9_16_transparent.png"),
            "is_checkerboard": False,
        })
        items.append({
            "id": "template_9_16_checkerboard.png",
            "name": "Mẫu cờ caro hướng dẫn (9:16 Dọc)",
            "path": os.path.join(templates_dir, "template_9_16_checkerboard.png"),
            "is_checkerboard": True,
        })
    else:
        items.append({
            "id": "none",
            "name": "Không dùng khung viền",
            "path": "",
            "is_checkerboard": False,
        })
        items.append({
            "id": "template_16_9_transparent.png",
            "name": "Khung bản tin thời sự (16:9 Ngang)",
            "path": os.path.join(templates_dir, "template_16_9_transparent.png"),
            "is_checkerboard": False,
        })
        items.append({
            "id": "template_16_9_checkerboard.png",
            "name": "Mẫu cờ caro hướng dẫn (16:9 Ngang)",
            "path": os.path.join(templates_dir, "template_16_9_checkerboard.png"),
            "is_checkerboard": True,
        })

    # Add any user uploaded templates in storage/templates
    default_ids = {
        "template_9_16_checkerboard.png",
        "template_9_16_transparent.png",
        "template_16_9_checkerboard.png",
        "template_16_9_transparent.png",
        "test_checkerboard.png",
        "test_badge.png",
    }
    try:
        for f in os.listdir(templates_dir):
            if f.endswith(".png") and f not in default_ids:
                full_path = os.path.join(templates_dir, f)
                items.append({
                    "id": f,
                    "name": f"Mẫu tải lên: {f}",
                    "path": full_path,
                    "is_checkerboard": False,
                })
    except Exception as exc:
        logger.debug(f"Failed to scan custom templates: {exc}")

    return items


def save_uploaded_template(file_bytes: bytes, filename: str) -> str:
    """Save an uploaded template file to storage/templates/ and return its path."""
    templates_dir = get_templates_dir()
    clean_name = os.path.basename(filename)
    if not clean_name.lower().endswith(".png"):
        clean_name += ".png"
    target_path = os.path.join(templates_dir, clean_name)
    with open(target_path, "wb") as f:
        f.write(file_bytes)
    logger.info(f"Saved custom template: {target_path}")
    return target_path
