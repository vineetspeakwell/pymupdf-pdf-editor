from flask import Flask, request, jsonify
from flask_cors import CORS
import fitz  # PyMuPDF
import base64
import os
import sqlite3
import json
import requests
import logging
import traceback
import math
from datetime import datetime

app = Flask(__name__)
CORS(app)

# -------------------------------------------------
# Logging
# -------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------------------------------
# Database Setup
# -------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "database.db")


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS pdf_capture_forms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pdf_id TEXT NOT NULL UNIQUE,
            user_id TEXT,
            enabled INTEGER DEFAULT 0,
            form_type TEXT DEFAULT 'popup',
            trigger_type TEXT DEFAULT 'on_open',
            trigger_value TEXT,
            headline TEXT,
            subheadline TEXT,
            button_text TEXT DEFAULT 'Submit',
            thank_you_message TEXT DEFAULT 'Thank you',
            fields_json TEXT,
            provider TEXT,
            provider_config_json TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS pdf_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pdf_id TEXT NOT NULL,
            form_id INTEGER,
            user_id TEXT,
            visitor_name TEXT,
            visitor_email TEXT,
            visitor_phone TEXT,
            custom_fields_json TEXT,
            source_url TEXT,
            ip_address TEXT,
            user_agent TEXT,
            provider TEXT,
            provider_status TEXT,
            provider_response TEXT,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()


# -------------------------------------------------
# Helpers
# -------------------------------------------------
def error_response(message, status=400):
    return jsonify({"success": False, "error": message}), status


def success_response(data=None, message=""):
    return jsonify({
        "success": True,
        "message": message,
        **(data or {})
    })


def parse_json():
    data = request.get_json(silent=True)
    if not data:
        raise ValueError("Invalid JSON body")
    return data


def now_str():
    return datetime.utcnow().isoformat()


def normalize_email(value):
    return (value or "").strip().lower()


def validate_email(value):
    value = normalize_email(value)
    return "@" in value and "." in value


def open_pdf_from_base64(pdf_base64):
    if not pdf_base64:
        raise ValueError("pdf_base64 is required")

    try:
        pdf_bytes = base64.b64decode(pdf_base64)
    except Exception as e:
        raise ValueError(f"Invalid base64 PDF data: {str(e)}")

    if not pdf_bytes:
        raise ValueError("Decoded PDF is empty")

    try:
        return fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise ValueError(f"Failed to open PDF: {str(e)}")


def doc_to_base64(doc):
    try:
        pdf_bytes = doc.tobytes(garbage=4, deflate=True)
        logger.info(f"PDF bytes generated: {len(pdf_bytes)} bytes")

        result = base64.b64encode(pdf_bytes).decode("utf-8")
        logger.info(f"Base64 encoded length: {len(result)} characters")
        return result
    except Exception as e:
        logger.error(f"Failed to encode PDF: {str(e)}", exc_info=True)
        raise


def safe_close_doc(doc):
    try:
        if doc is not None:
            doc.close()
    except Exception:
        pass


def normalize_text(s):
    if s is None:
        return ""
    return " ".join(str(s).split())


def int_to_rgb_tuple(color_int):
    try:
        if color_int is None:
            return (0, 0, 0)
        r = (int(color_int) >> 16) & 255
        g = (int(color_int) >> 8) & 255
        b = int(color_int) & 255
        return (r / 255.0, g / 255.0, b / 255.0)
    except Exception:
        return (0, 0, 0)


def rgb_distance(c1, c2):
    return math.sqrt(
        ((c1[0] - c2[0]) ** 2) +
        ((c1[1] - c2[1]) ** 2) +
        ((c1[2] - c2[2]) ** 2)
    )


def clamp_rect_to_page(rect, page_rect):
    x0 = max(page_rect.x0, min(rect.x0, page_rect.x1))
    y0 = max(page_rect.y0, min(rect.y0, page_rect.y1))
    x1 = max(page_rect.x0, min(rect.x1, page_rect.x1))
    y1 = max(page_rect.y0, min(rect.y1, page_rect.y1))
    return fitz.Rect(x0, y0, x1, y1)


def expand_rect(rect, pad=2):
    return fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)


def text_length_with_font(font, text, fontsize):
    try:
        return font.text_length(text, fontsize=fontsize)
    except Exception:
        try:
            return fitz.get_text_length(text, fontname="helv", fontsize=fontsize)
        except Exception:
            return len(text) * fontsize * 0.55


def fit_font_size_to_rect(text, rect, font_obj, start_size, min_size=5):
    size = max(min_size, float(start_size))
    max_width = max(1, rect.width)
    max_height = max(1, rect.height)

    while size >= min_size:
        lines = wrap_text_to_rect(text, font_obj, size, max_width)
        line_height = size * 1.2
        total_height = len(lines) * line_height

        fits_width = True
        for line in lines:
            if text_length_with_font(font_obj, line, size) > max_width + 0.5:
                fits_width = False
                break

        if fits_width and total_height <= max_height + 0.5:
            return size, lines

        size -= 0.5

    lines = wrap_text_to_rect(text, font_obj, min_size, max_width)
    return min_size, lines


def wrap_text_to_rect(text, font_obj, fontsize, max_width):
    if text is None:
        return [""]

    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = text.split("\n")
    all_lines = []

    for para in paragraphs:
        para = para.strip()

        if para == "":
            all_lines.append("")
            continue

        words = para.split()
        current = ""

        for word in words:
            candidate = word if not current else f"{current} {word}"
            if text_length_with_font(font_obj, candidate, fontsize) <= max_width:
                current = candidate
            else:
                if current:
                    all_lines.append(current)
                    current = ""

                if text_length_with_font(font_obj, word, fontsize) <= max_width:
                    current = word
                else:
                    broken = break_long_word(word, font_obj, fontsize, max_width)
                    if broken:
                        all_lines.extend(broken[:-1])
                        current = broken[-1]
                    else:
                        current = word

        if current:
            all_lines.append(current)

    if not all_lines:
        return [""]

    return all_lines


def break_long_word(word, font_obj, fontsize, max_width):
    parts = []
    current = ""
    for ch in word:
        candidate = current + ch
        if text_length_with_font(font_obj, candidate, fontsize) <= max_width:
            current = candidate
        else:
            if current:
                parts.append(current)
            current = ch
    if current:
        parts.append(current)
    return parts


def get_font_variant(base_fontname, is_bold=False, is_italic=False):
    base = (base_fontname or "").lower()

    if "times" in base or base in ["tiro", "times-roman", "times roman"]:
        if is_bold and is_italic:
            return "times-bolditalic"
        if is_bold:
            return "times-bold"
        if is_italic:
            return "times-italic"
        return "times-roman"

    if "courier" in base or "cour" in base:
        if is_bold and is_italic:
            return "courier-boldoblique"
        if is_bold:
            return "courier-bold"
        if is_italic:
            return "courier-oblique"
        return "courier"

    if is_bold and is_italic:
        return "helv-boldoblique"
    if is_bold:
        return "helv-bold"
    if is_italic:
        return "helv-oblique"
    return "helv"


def get_font_object(fontname):
    try:
        return fitz.Font(fontname=fontname)
    except Exception:
        try:
            return fitz.Font(fontname="helv")
        except Exception:
            return None


def extract_drawings_background_color(page, target_rect):
    try:
        drawings = page.get_drawings()
    except Exception as e:
        logger.warning(f"get_drawings failed: {str(e)}")
        return None

    best_color = None
    best_score = None
    target_center = fitz.Point(
        (target_rect.x0 + target_rect.x1) / 2,
        (target_rect.y0 + target_rect.y1) / 2
    )

    for d in drawings:
        fill = d.get("fill")
        rect = d.get("rect")
        if fill is None or rect is None:
            continue

        try:
            draw_rect = fitz.Rect(rect)
        except Exception:
            continue

        expanded = expand_rect(target_rect, 3)
        intersects = draw_rect.intersects(expanded)
        contains = draw_rect.contains(target_rect)

        if not intersects and not contains:
            continue

        center = fitz.Point(
            (draw_rect.x0 + draw_rect.x1) / 2,
            (draw_rect.y0 + draw_rect.y1) / 2
        )
        dist = math.sqrt((center.x - target_center.x) ** 2 + (center.y - target_center.y) ** 2)

        score = dist
        if contains:
            score -= 1000

        if best_score is None or score < best_score:
            best_score = score
            best_color = fill

    return best_color


def extract_span_candidates(page, target_rect):
    candidates = []

    try:
        text_dict = page.get_text("dict")
    except Exception as e:
        logger.warning(f"page.get_text('dict') failed: {str(e)}")
        return candidates

    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bbox = span.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue

                span_rect = fitz.Rect(bbox)
                if not span_rect.intersects(target_rect):
                    continue

                flags = int(span.get("flags", 0))
                size = float(span.get("size", 11))
                font_raw = span.get("font", "helv")
                color = int_to_rgb_tuple(span.get("color"))

                font_name_lower = str(font_raw).lower()
                is_bold = ("bold" in font_name_lower) or bool(flags & 16)
                is_italic = (
                    "italic" in font_name_lower or
                    "oblique" in font_name_lower or
                    bool(flags & 2)
                )

                safe_fontname = get_font_variant(font_raw, is_bold=is_bold, is_italic=is_italic)

                candidates.append({
                    "bbox": span_rect,
                    "text": span.get("text", ""),
                    "size": size,
                    "font_raw": font_raw,
                    "fontname": safe_fontname,
                    "color": color,
                    "flags": flags,
                    "is_bold": is_bold,
                    "is_italic": is_italic
                })

    return candidates


def choose_best_style_for_rect(page, target_rect, old_text):
    candidates = extract_span_candidates(page, target_rect)
    old_norm = normalize_text(old_text).lower()

    chosen = None

    if candidates:
        for c in candidates:
            if old_norm and old_norm in normalize_text(c["text"]).lower():
                chosen = c
                break

    if chosen is None and candidates:
        best = None
        best_area = -1
        for c in candidates:
            inter = c["bbox"] & target_rect
            area = max(0, inter.width) * max(0, inter.height)
            if area > best_area:
                best_area = area
                best = c
        chosen = best

    if chosen is None:
        chosen = {
            "size": 11,
            "fontname": "helv",
            "color": (0, 0, 0),
            "is_bold": False,
            "is_italic": False
        }

    bg_color = extract_drawings_background_color(page, target_rect)
    if bg_color is None:
        txt = chosen.get("color", (0, 0, 0))
        if rgb_distance(txt, (1, 1, 1)) < 0.25:
            bg_color = (0.95, 0.95, 0.95)
        else:
            bg_color = (1, 1, 1)

    return {
        "fontsize": max(5, float(chosen.get("size", 11))),
        "fontname": chosen.get("fontname", "helv"),
        "color": chosen.get("color", (0, 0, 0)),
        "is_bold": bool(chosen.get("is_bold", False)),
        "is_italic": bool(chosen.get("is_italic", False)),
        "background_color": bg_color
    }


def insert_replacement_text(page, rect, text, style):
    text = "" if text is None else str(text)
    if text == "":
        return True

    page_rect = page.rect
    rect = clamp_rect_to_page(rect, page_rect)

    padded = fitz.Rect(rect.x0 + 1.5, rect.y0 + 1.0, rect.x1 - 1.5, rect.y1 - 1.0)
    if padded.width <= 2 or padded.height <= 2:
        padded = rect

    fontsize = float(style.get("fontsize", 11))
    fontname = style.get("fontname", "helv")
    color = style.get("color", (0, 0, 0))

    font_obj = get_font_object(fontname)
    if font_obj is None:
        fontname = "helv"
        font_obj = get_font_object(fontname)

    final_size, lines = fit_font_size_to_rect(
        text=text,
        rect=padded,
        font_obj=font_obj,
        start_size=fontsize,
        min_size=5
    )

    line_height = final_size * 1.2
    y = padded.y0 + final_size

    for line in lines:
        if y > padded.y1 + 0.5:
            break

        try:
            page.insert_text(
                (padded.x0, y),
                line,
                fontsize=final_size,
                fontname=fontname,
                color=color
            )
        except Exception as e:
            logger.warning(f"insert_text with font '{fontname}' failed: {str(e)}")
            try:
                page.insert_text(
                    (padded.x0, y),
                    line,
                    fontsize=final_size,
                    fontname="helv",
                    color=color
                )
            except Exception as e2:
                logger.warning(f"insert_text fallback helv failed: {str(e2)}")
                return False

        y += line_height

    return True


# -------------------------------------------------
# CRM Provider Functions
# -------------------------------------------------
def send_to_mailchimp(config, lead_data):
    api_key = config.get("api_key", "").strip()
    audience_id = config.get("audience_id", "").strip()
    status = config.get("status", "subscribed")

    if not api_key or not audience_id:
        raise Exception("Mailchimp api_key and audience_id are required")

    if "-" not in api_key:
        raise Exception("Invalid Mailchimp API key format")

    dc = api_key.split("-")[-1]
    url = f"https://{dc}.api.mailchimp.com/3.0/lists/{audience_id}/members"

    payload = {
        "email_address": lead_data["email"],
        "status": status,
        "merge_fields": {}
    }

    if lead_data.get("name"):
        payload["merge_fields"]["FNAME"] = lead_data.get("name")

    if lead_data.get("phone"):
        payload["merge_fields"]["PHONE"] = lead_data.get("phone")

    response = requests.post(
        url,
        auth=("anystring", api_key),
        json=payload,
        timeout=20
    )

    return {
        "status_code": response.status_code,
        "response_text": response.text
    }


def send_to_getresponse(config, lead_data):
    api_key = config.get("api_key", "").strip()
    campaign_id = config.get("campaign_id", "").strip()

    if not api_key or not campaign_id:
        raise Exception("GetResponse api_key and campaign_id are required")

    url = "https://api.getresponse.com/v3/contacts"

    payload = {
        "email": lead_data["email"],
        "campaign": {
            "campaignId": campaign_id
        }
    }

    if lead_data.get("name"):
        payload["name"] = lead_data["name"]

    response = requests.post(
        url,
        headers={
            "X-Auth-Token": f"api-key {api_key}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=20
    )

    return {
        "status_code": response.status_code,
        "response_text": response.text
    }


def send_to_webhook(config, lead_data):
    webhook_url = config.get("webhook_url", "").strip()

    if not webhook_url:
        raise Exception("webhook_url is required")

    response = requests.post(
        webhook_url,
        json=lead_data,
        timeout=20
    )

    return {
        "status_code": response.status_code,
        "response_text": response.text
    }


def send_to_systeme(config, lead_data):
    webhook_url = config.get("webhook_url", "").strip()
    if not webhook_url:
        raise Exception("For now, Systeme.io requires webhook_url")
    return send_to_webhook({"webhook_url": webhook_url}, lead_data)


def send_to_aweber(config, lead_data):
    webhook_url = config.get("webhook_url", "").strip()
    if not webhook_url:
        raise Exception("For now, AWeber requires webhook_url")
    return send_to_webhook({"webhook_url": webhook_url}, lead_data)


def send_to_sendfox(config, lead_data):
    webhook_url = config.get("webhook_url", "").strip()
    if not webhook_url:
        raise Exception("For now, SendFox requires webhook_url")
    return send_to_webhook({"webhook_url": webhook_url}, lead_data)


def send_lead_to_provider(provider, config, lead_data):
    provider = (provider or "").strip().lower()

    if provider == "mailchimp":
        return send_to_mailchimp(config, lead_data)
    elif provider == "getresponse":
        return send_to_getresponse(config, lead_data)
    elif provider == "systeme":
        return send_to_systeme(config, lead_data)
    elif provider == "aweber":
        return send_to_aweber(config, lead_data)
    elif provider == "sendfox":
        return send_to_sendfox(config, lead_data)
    elif provider == "webhook":
        return send_to_webhook(config, lead_data)
    elif provider == "":
        return {
            "status_code": 200,
            "response_text": "No provider selected. Lead stored locally only."
        }
    else:
        raise Exception(f"Unsupported provider: {provider}")


# -------------------------------------------------
# Basic Routes
# -------------------------------------------------
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "success": True,
        "message": "Advanced PDF Editor + Capture Form API is running"
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "success": True,
        "status": "ok"
    })


# -------------------------------------------------
# PDF Editor Routes
# -------------------------------------------------
@app.route("/find-replace", methods=["POST"])
def find_replace():
    doc = None
    try:
        logger.info("Received find-replace request")
        data = parse_json()
        replacements = data.get("replacements", [])

        if not isinstance(replacements, list):
            return error_response("replacements must be a list")

        doc = open_pdf_from_base64(data.get("pdf_base64"))
        logger.info(f"PDF opened successfully, pages: {len(doc)}")

        for page_index, page in enumerate(doc):
            redaction_items = []
            text_insertions = []

            for replacement in replacements:
                if not isinstance(replacement, dict):
                    continue

                old_text = replacement.get("old_text")
                new_text = replacement.get("new_text")

                if not old_text or new_text is None:
                    continue

                try:
                    matches = page.search_for(str(old_text))
                except Exception as e:
                    logger.warning(
                        f"search_for failed on page {page_index} for '{old_text}': {str(e)}"
                    )
                    continue

                if not matches:
                    continue

                for rect in matches:
                    style = choose_best_style_for_rect(page, rect, old_text)

                    redaction_items.append({
                        "rect": rect,
                        "fill": style["background_color"]
                    })

                    text_insertions.append({
                        "rect": rect,
                        "text": str(new_text),
                        "style": style
                    })

                    logger.info(
                        f"Page {page_index}: match '{old_text}' at {rect}, "
                        f"font={style['fontname']}, size={style['fontsize']}"
                    )

            for item in redaction_items:
                page.add_redact_annot(item["rect"], fill=item["fill"])

            if redaction_items:
                page.apply_redactions()
                logger.info(
                    f"Page {page_index}: applied {len(redaction_items)} redactions"
                )

            for item in text_insertions:
                ok = insert_replacement_text(
                    page=page,
                    rect=item["rect"],
                    text=item["text"],
                    style=item["style"]
                )
                if not ok:
                    logger.warning(
                        f"Page {page_index}: failed inserting replacement text"
                    )

        result_pdf_base64 = doc_to_base64(doc)
        safe_close_doc(doc)
        doc = None

        logger.info("Find-replace completed successfully")
        return jsonify({
            "success": True,
            "pdf_base64": result_pdf_base64
        })

    except Exception as e:
        logger.error(f"Find-replace error: {str(e)}", exc_info=True)
        safe_close_doc(doc)
        return error_response(f"find-replace failed: {str(e)}", 500)


@app.route("/replace-image", methods=["POST"])
def replace_image():
    doc = None
    try:
        logger.info("Received replace-image request")
        data = parse_json()

        image_base64 = data.get("image_base64")
        page_num = int(data.get("page_num", 0))
        x = float(data.get("x", 0))
        y = float(data.get("y", 0))
        width = float(data.get("width", 100))
        height = float(data.get("height", 100))

        if not image_base64:
            return error_response("image_base64 is required")

        if width <= 0 or height <= 0:
            return error_response("width and height must be greater than 0")

        try:
            image_bytes = base64.b64decode(image_base64)
        except Exception as e:
            return error_response(f"Invalid image_base64: {str(e)}")

        logger.info(f"Image decoded: {len(image_bytes)} bytes")

        doc = open_pdf_from_base64(data.get("pdf_base64"))

        if page_num < 0 or page_num >= len(doc):
            safe_close_doc(doc)
            return error_response("Invalid page_num")

        page = doc[page_num]
        page_rect = page.rect
        logger.info(
            f"Page {page_num} dimensions: {page_rect.width} x {page_rect.height}"
        )

        x = max(0, min(x, page_rect.width - width))
        y = max(0, min(y, page_rect.height - height))

        rect = fitz.Rect(x, y, x + width, y + height)
        logger.info(f"Inserting image at: {rect}")

        page.insert_image(rect, stream=image_bytes, keep_proportion=True)

        result_pdf_base64 = doc_to_base64(doc)
        safe_close_doc(doc)
        doc = None

        logger.info("Image replacement completed successfully")
        return jsonify({
            "success": True,
            "pdf_base64": result_pdf_base64
        })

    except Exception as e:
        logger.error(f"Replace-image error: {str(e)}", exc_info=True)
        safe_close_doc(doc)
        return error_response(f"replace-image failed: {str(e)}", 500)


@app.route("/scan-links", methods=["POST"])
def scan_links():
    doc = None
    try:
        logger.info("Received scan-links request")
        data = parse_json()
        doc = open_pdf_from_base64(data.get("pdf_base64"))

        all_links = []
        for page_index, page in enumerate(doc):
            links = page.get_links()
            for link in links:
                uri = link.get("uri")
                rect = link.get("from")
                if uri:
                    all_links.append({
                        "page": page_index,
                        "uri": uri,
                        "rect": {
                            "x0": rect.x0,
                            "y0": rect.y0,
                            "x1": rect.x1,
                            "y1": rect.y1
                        } if rect else None
                    })

        safe_close_doc(doc)
        doc = None

        logger.info(f"Found {len(all_links)} links")
        return jsonify({
            "success": True,
            "count": len(all_links),
            "links": all_links
        })

    except Exception as e:
        logger.error(f"Scan-links error: {str(e)}", exc_info=True)
        safe_close_doc(doc)
        return error_response(f"scan-links failed: {str(e)}", 500)


@app.route("/replace-links", methods=["POST"])
def replace_links():
    doc = None
    try:
        logger.info("Received replace-links request")
        data = parse_json()

        replacements = data.get("replacements", [])
        if not isinstance(replacements, list):
            return error_response("replacements must be a list")

        doc = open_pdf_from_base64(data.get("pdf_base64"))

        for page_index, page in enumerate(doc):
            links = page.get_links()
            if not links:
                continue

            for link in links:
                old_uri = link.get("uri")
                if not old_uri:
                    continue

                for replacement in replacements:
                    if not isinstance(replacement, dict):
                        continue

                    old_link = replacement.get("old_link")
                    new_link = replacement.get("new_link")

                    if not old_link or not new_link:
                        continue

                    if old_uri == old_link:
                        rect = link.get("from")
                        if not rect:
                            continue

                        try:
                            page.insert_link({
                                "kind": fitz.LINK_URI,
                                "from": rect,
                                "uri": new_link
                            })
                            logger.info(
                                f"Page {page_index}: replaced link {old_link} -> {new_link}"
                            )
                        except Exception as e:
                            logger.warning(
                                f"Failed replacing link on page {page_index}: {str(e)}"
                            )

        result_pdf_base64 = doc_to_base64(doc)
        safe_close_doc(doc)
        doc = None

        return jsonify({
            "success": True,
            "pdf_base64": result_pdf_base64
        })

    except Exception as e:
        logger.error(f"Replace-links error: {str(e)}", exc_info=True)
        safe_close_doc(doc)
        return error_response(f"replace-links failed: {str(e)}", 500)


@app.route("/add-annotation", methods=["POST"])
def add_annotation():
    doc = None
    try:
        logger.info("Received add-annotation request")
        data = parse_json()

        page_num = int(data.get("page_num", 0))
        annotation_type = data.get("type", "text")
        content = data.get("content", "")
        x = float(data.get("x", 0))
        y = float(data.get("y", 0))

        doc = open_pdf_from_base64(data.get("pdf_base64"))

        if page_num < 0 or page_num >= len(doc):
            safe_close_doc(doc)
            return error_response("Invalid page_num")

        page = doc[page_num]

        if annotation_type == "text":
            page.insert_text(
                (x, y),
                content,
                fontsize=12,
                color=(0, 0, 0)
            )

        result_pdf_base64 = doc_to_base64(doc)
        safe_close_doc(doc)
        doc = None

        return jsonify({
            "success": True,
            "pdf_base64": result_pdf_base64
        })

    except Exception as e:
        logger.error(f"Add-annotation error: {str(e)}", exc_info=True)
        safe_close_doc(doc)
        return error_response(f"add-annotation failed: {str(e)}", 500)


@app.route("/add-stamp", methods=["POST"])
def add_stamp():
    doc = None
    try:
        logger.info("Received add-stamp request")
        data = parse_json()

        page_num = int(data.get("page_num", 0))
        stamp_text = data.get("stamp_text", "DRAFT")
        x = float(data.get("x", 100))
        y = float(data.get("y", 100))
        color = data.get("color", "#FF0000")
        rotation = int(data.get("rotation", 0))

        if rotation not in [0, 90, 180, 270]:
            return error_response("rotation must be 0, 90, 180, or 270")

        color_rgb = tuple(
            int(color.lstrip("#")[i:i + 2], 16) / 255.0 for i in (0, 2, 4)
        )

        doc = open_pdf_from_base64(data.get("pdf_base64"))

        if page_num < 0 or page_num >= len(doc):
            safe_close_doc(doc)
            return error_response("Invalid page_num")

        page = doc[page_num]
        page.insert_text(
            (x, y),
            stamp_text,
            fontsize=36,
            color=color_rgb,
            rotate=rotation
        )

        result_pdf_base64 = doc_to_base64(doc)
        safe_close_doc(doc)
        doc = None

        return jsonify({
            "success": True,
            "pdf_base64": result_pdf_base64
        })

    except Exception as e:
        logger.error(f"Add-stamp error: {str(e)}", exc_info=True)
        safe_close_doc(doc)
        return error_response(f"add-stamp failed: {str(e)}", 500)


@app.route("/add-watermark", methods=["POST"])
def add_watermark():
    doc = None
    try:
        logger.info("Received add-watermark request")
        data = parse_json()

        watermark_text = data.get("text", "CONFIDENTIAL")

        doc = open_pdf_from_base64(data.get("pdf_base64"))

        for page in doc:
            rect = page.rect
            box = fitz.Rect(
                rect.width * 0.15,
                rect.height * 0.45,
                rect.width * 0.85,
                rect.height * 0.60
            )
            page.insert_textbox(
                box,
                watermark_text,
                fontsize=40,
                color=(0.7, 0.7, 0.7),
                align=1
            )

        result_pdf_base64 = doc_to_base64(doc)
        safe_close_doc(doc)
        doc = None

        return jsonify({
            "success": True,
            "pdf_base64": result_pdf_base64
        })

    except Exception as e:
        logger.error(f"Add-watermark error: {str(e)}", exc_info=True)
        safe_close_doc(doc)
        return error_response(f"add-watermark failed: {str(e)}", 500)


# -------------------------------------------------
# Capture Form Routes
# -------------------------------------------------
@app.route("/capture-form/save", methods=["POST"])
def save_capture_form():
    try:
        data = parse_json()

        pdf_id = str(data.get("pdf_id", "")).strip()
        user_id = str(data.get("user_id", "")).strip()
        enabled = 1 if data.get("enabled", False) else 0
        form_type = str(data.get("form_type", "popup")).strip()
        trigger_type = str(data.get("trigger_type", "on_open")).strip()
        trigger_value = str(data.get("trigger_value", "")).strip()
        headline = str(data.get("headline", "Unlock this PDF")).strip()
        subheadline = str(data.get("subheadline", "Enter your details")).strip()
        button_text = str(data.get("button_text", "Submit")).strip()
        thank_you_message = str(data.get("thank_you_message", "Thank you")).strip()
        fields_json = json.dumps(data.get("fields", []))
        provider = str(data.get("provider", "")).strip().lower()
        provider_config_json = json.dumps(data.get("provider_config", {}))
        updated_at = now_str()

        if not pdf_id:
            return error_response("pdf_id is required", 400)

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT id FROM pdf_capture_forms WHERE pdf_id = ?", (pdf_id,))
        existing = cur.fetchone()

        if existing:
            cur.execute("""
                UPDATE pdf_capture_forms
                SET user_id = ?, enabled = ?, form_type = ?, trigger_type = ?, trigger_value = ?,
                    headline = ?, subheadline = ?, button_text = ?, thank_you_message = ?,
                    fields_json = ?, provider = ?, provider_config_json = ?, updated_at = ?
                WHERE pdf_id = ?
            """, (
                user_id, enabled, form_type, trigger_type, trigger_value,
                headline, subheadline, button_text, thank_you_message,
                fields_json, provider, provider_config_json, updated_at, pdf_id
            ))
        else:
            created_at = updated_at
            cur.execute("""
                INSERT INTO pdf_capture_forms (
                    pdf_id, user_id, enabled, form_type, trigger_type, trigger_value,
                    headline, subheadline, button_text, thank_you_message,
                    fields_json, provider, provider_config_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pdf_id, user_id, enabled, form_type, trigger_type, trigger_value,
                headline, subheadline, button_text, thank_you_message,
                fields_json, provider, provider_config_json, created_at, updated_at
            ))

        conn.commit()
        conn.close()

        return success_response(message="Capture form saved successfully")

    except Exception as e:
        return error_response(f"Failed to save capture form: {str(e)}", 500)


@app.route("/capture-form/<pdf_id>", methods=["GET"])
def get_capture_form(pdf_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT * FROM pdf_capture_forms WHERE pdf_id = ?", (pdf_id,))
        row = cur.fetchone()
        conn.close()

        if not row:
            return error_response("Capture form not found", 404)

        result = dict(row)
        result["fields"] = json.loads(result.get("fields_json") or "[]")
        result["provider_config"] = json.loads(result.get("provider_config_json") or "{}")
        result.pop("fields_json", None)
        result.pop("provider_config_json", None)

        return success_response(data={"data": result})

    except Exception as e:
        return error_response(f"Failed to fetch capture form: {str(e)}", 500)


@app.route("/capture-form/submit", methods=["POST"])
def submit_capture_form():
    try:
        data = parse_json()

        pdf_id = str(data.get("pdf_id", "")).strip()
        name = str(data.get("name", "")).strip()
        email = normalize_email(data.get("email", ""))
        phone = str(data.get("phone", "")).strip()
        custom_fields = data.get("custom_fields", {})
        source_url = str(data.get("source_url", "")).strip()

        if not pdf_id:
            return error_response("pdf_id is required", 400)

        if not email:
            return error_response("email is required", 400)

        if not validate_email(email):
            return error_response("Invalid email address", 400)

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT * FROM pdf_capture_forms WHERE pdf_id = ?", (pdf_id,))
        form_row = cur.fetchone()

        if not form_row:
            conn.close()
            return error_response("No capture form configured", 404)

        form_data = dict(form_row)
        provider = form_data.get("provider", "")
        provider_config = json.loads(form_data.get("provider_config_json") or "{}")

        lead_payload = {
            "pdf_id": pdf_id,
            "email": email,
            "custom_fields": custom_fields,
            "source_url": source_url
        }

        if name:
            lead_payload["name"] = name

        if phone:
            lead_payload["phone"] = phone

        provider_status = "pending"
        provider_response = ""

        try:
            provider_result = send_lead_to_provider(provider, provider_config, lead_payload)
            status_code = provider_result.get("status_code", 500)
            provider_response = provider_result.get("response_text", "")

            if 200 <= status_code < 300:
                provider_status = "success"
            else:
                provider_status = "failed"
        except Exception as provider_error:
            provider_status = "failed"
            provider_response = str(provider_error)

        cur.execute("""
            INSERT INTO pdf_leads (
                pdf_id, form_id, user_id, visitor_name, visitor_email, visitor_phone,
                custom_fields_json, source_url, ip_address, user_agent,
                provider, provider_status, provider_response, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pdf_id,
            form_data.get("id"),
            form_data.get("user_id"),
            name,
            email,
            phone,
            json.dumps(custom_fields),
            source_url,
            request.headers.get("X-Forwarded-For", request.remote_addr),
            request.headers.get("User-Agent", ""),
            provider,
            provider_status,
            provider_response,
            now_str()
        ))

        conn.commit()
        conn.close()

        if provider_status == "success" or provider == "":
            return success_response(
                data={"provider_status": provider_status},
                message="Lead submitted successfully"
            )

        return jsonify({
            "success": False,
            "message": "Lead saved but provider failed",
            "provider_status": provider_status,
            "provider_response": provider_response
        }), 500

    except Exception as e:
        return error_response(f"Failed to submit lead: {str(e)}", 500)


@app.route("/capture-form/leads/<pdf_id>", methods=["GET"])
def get_pdf_leads(pdf_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT * FROM pdf_leads
            WHERE pdf_id = ?
            ORDER BY id DESC
        """, (pdf_id,))

        rows = cur.fetchall()
        conn.close()

        leads = []
        for row in rows:
            item = dict(row)
            item["custom_fields"] = json.loads(item.get("custom_fields_json") or "{}")
            item.pop("custom_fields_json", None)
            leads.append(item)

        return success_response(
            data={"count": len(leads), "leads": leads}
        )

    except Exception as e:
        return error_response(f"Failed to fetch leads: {str(e)}", 500)


@app.route("/capture-form/test-provider", methods=["POST"])
def test_provider():
    try:
        data = parse_json()
        provider = str(data.get("provider", "")).strip().lower()
        provider_config = data.get("provider_config", {})

        test_lead = {
            "email": "test@example.com",
            "name": "Test Lead",
            "phone": "1234567890"
        }

        result = send_lead_to_provider(provider, provider_config, test_lead)
        status_code = result.get("status_code", 500)

        if 200 <= status_code < 300:
            return success_response(data=result, message="Provider test successful")

        return jsonify({
            "success": False,
            "message": "Provider test failed",
            **result
        }), 500

    except Exception as e:
        return error_response(f"Provider test failed: {str(e)}", 500)


# -------------------------------------------------
# Error Handlers
# -------------------------------------------------
@app.errorhandler(404)
def not_found(_e):
    return error_response("Route not found", 404)


@app.errorhandler(500)
def internal_error(_e):
    logger.error("Unhandled server error:\n%s", traceback.format_exc())
    return error_response("Internal server error", 500)


# -------------------------------------------------
# Start App
# -------------------------------------------------
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)



# -------------------------------------------------
# Password Protect PDF Route
# -------------------------------------------------
@app.route("/password-protect", methods=["POST"])
def password_protect_pdf():
    doc = None
    try:
        data = parse_json()
        pdf_base64 = data.get("pdf_base64")
        user_password = str(data.get("user_password", "")).strip()
        owner_password = str(data.get("owner_password", user_password)).strip()

        if not pdf_base64 or not user_password:
            return jsonify({
                "success": False,
                "error": "PDF data and user password are required"
            }), 400

        # Decode PDF
        try:
            pdf_bytes = base64.b64decode(pdf_base64)
        except Exception as e:
            return jsonify({
                "success": False,
                "error": f"Invalid base64 PDF data: {str(e)}"
            }), 400

        # Open PDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        # Permissions
        perm = int(
            fitz.PDF_PERM_ACCESSIBILITY |
            fitz.PDF_PERM_PRINT |
            fitz.PDF_PERM_COPY |
            fitz.PDF_PERM_ANNOTATE
        )

        # Encrypt using AES-256
        protected_bytes = doc.tobytes(
            garbage=4,
            deflate=True,
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw=owner_password,
            user_pw=user_password,
            permissions=perm
        )

        safe_close_doc(doc)
        doc = None

        protected_base64 = base64.b64encode(protected_bytes).decode("utf-8")

        return jsonify({
            "success": True,
            "pdf_base64": protected_base64
        })

    except Exception as e:
        logger.error(f"Password protect error: {str(e)}", exc_info=True)
        safe_close_doc(doc)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# -------------------------------------------------
# Error Handlers
# -------------------------------------------------
@app.errorhandler(404)
def not_found(_e):
    return error_response("Route not found", 404)


@app.errorhandler(500)
def internal_error(_e):
    logger.error("Unhandled server error:\n%s", traceback.format_exc())
    return error_response("Internal server error", 500)


# -------------------------------------------------
# Start App
# -------------------------------------------------
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)



@app.route('/pdf-to-html', methods=['POST'])
def pdf_to_html():
"""Convert PDF to HTML for editing"""
try:
    data = request.json
    pdf_base64 = data.get('pdf_base64')
    
    if not pdf_base64:
        return jsonify({'error': 'Missing pdf_base64'}), 400
    
    # Decode PDF
    pdf_bytes = base64.b64decode(pdf_base64)
    
    # Open PDF with PyMuPDF
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    
    # Extract text from all pages with formatting
    html_parts = ['<div style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto;">']
    
    for page_num in range(len(doc)):
        page = doc[page_num]
        
        # Get text with formatting
        blocks = page.get_text("dict")["blocks"]
        
        html_parts.append(f'<div style="page-break-after: always; padding: 40px 0;">')
        
        for block in blocks:
            if block.get("type") == 0:  # Text block
                for line in block.get("lines", []):
                    line_html = '<p style="margin: 8px 0;">'
                    for span in line.get("spans", []):
                        text = span.get("text", "")
                        font_size = span.get("size", 12)
                        font_name = span.get("font", "Arial")
                        color = span.get("color", 0)
                        
                        # Convert color from int to hex
                        r = (color >> 16) & 0xFF
                        g = (color >> 8) & 0xFF
                        b = color & 0xFF
                        color_hex = f"#{r:02x}{g:02x}{b:02x}"
                        
                        # Check if bold
                        is_bold = "bold" in font_name.lower() or span.get("flags", 0) & 16
                        
                        style = f'font-size: {font_size}px; color: {color_hex};'
                        if is_bold:
                            style += ' font-weight: bold;'
                        
                        line_html += f'<span style="{style}">{text}</span>'
                    
                    line_html += '</p>'
                    html_parts.append(line_html)
        
        html_parts.append('</div>')
    
    html_parts.append('</div>')
    html_content = '\n'.join(html_parts)
    
    doc.close()
    
    return jsonify({
        'html': html_content,
        'success': True
    })
    
except Exception as e:
    print(f"Error converting PDF to HTML: {str(e)}")
    return jsonify({'error': str(e)}), 500


@app.route('/html-to-pdf', methods=['POST'])
def html_to_pdf():
"""Convert HTML to PDF"""
try:
    data = request.json
    html_content = data.get('html_content')
    
    if not html_content:
        return jsonify({'error': 'Missing html_content'}), 400
    
    # Create a new PDF document
    doc = fitz.open()
    
    # Create page with standard letter size (612x792 points)
    page = doc.new_page(width=612, height=792)
    
    # Strip HTML tags and extract plain text for now
    # For a production system, you'd want a proper HTML renderer
    import re
    text = re.sub('<[^<]+?>', '', html_content)
    text = text.replace('&nbsp;', ' ')
    
    # Insert text into PDF
    rect = fitz.Rect(72, 72, 540, 720)  # 1 inch margins
    page.insert_textbox(
        rect,
        text,
        fontsize=12,
        fontname="helv",
        align=fitz.TEXT_ALIGN_LEFT
    )
    
    # Convert to bytes
    pdf_bytes = doc.tobytes()
    pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
    
    doc.close()
    
    return jsonify({
        'pdf_base64': pdf_base64,
        'success': True
    })
    
except Exception as e:
    print(f"Error converting HTML to PDF: {str(e)}")
    return jsonify({'error': str(e)}), 500
