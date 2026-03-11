from flask import Flask, request, jsonify
from flask_cors import CORS
import fitz  # PyMuPDF
import base64
import os
import sqlite3
import json
import requests
from datetime import datetime

app = Flask(__name__)
CORS(app)

DB_PATH = os.environ.get("DB_PATH", "database.db")


# =========================================================
# DATABASE
# =========================================================
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


# =========================================================
# COMMON HELPERS
# =========================================================
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


# =========================================================
# PDF HELPERS
# =========================================================
def open_pdf_from_base64(pdf_base64):
    if not pdf_base64:
        raise ValueError("pdf_base64 is required")
    pdf_bytes = base64.b64decode(pdf_base64)
    return fitz.open(stream=pdf_bytes, filetype="pdf")


def doc_to_base64(doc):
    pdf_bytes = doc.tobytes(garbage=4, deflate=True)
    return base64.b64encode(pdf_bytes).decode("utf-8")


# =========================================================
# CAPTURE FORM PROVIDERS
# =========================================================
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
        "merge_fields": {
            "FNAME": lead_data.get("name", "")
        }
    }

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
        "name": lead_data.get("name", ""),
        "campaign": {
            "campaignId": campaign_id
        }
    }

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


# =========================================================
# ROOT + HEALTH
# =========================================================
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "success": True,
        "message": "PDF Editor API + Capture Form API is running",
        "routes": [
            "/health",
            "/find-replace",
            "/save-text-at-rect",
            "/replace-image",
            "/add-annotation",
            "/add-stamp",
            "/add-watermark",
            "/scan-links",
            "/replace-links",
            "/capture-form/save",
            "/capture-form/<pdf_id>",
            "/capture-form/submit",
            "/capture-form/leads/<pdf_id>",
            "/capture-form/test-provider"
        ]
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"success": True, "status": "ok"})


# =========================================================
# PDF EDITOR ROUTES
# =========================================================
@app.route("/find-replace", methods=["POST"])
def find_replace():
    try:
        data = parse_json()
        replacements = data.get("replacements", [])
        doc = open_pdf_from_base64(data.get("pdf_base64"))

        for page in doc:
            for replacement in replacements:
                old_text = replacement.get("old_text")
                new_text = replacement.get("new_text")

                if not old_text or new_text is None:
                    continue

                matches = page.search_for(old_text)
                if not matches:
                    continue

                for rect in matches:
                    page.add_redact_annot(rect, fill=(1, 1, 1))
                page.apply_redactions()

                for rect in matches:
                    page.insert_textbox(
                        rect,
                        new_text,
                        fontsize=12,
                        color=(0, 0, 0),
                        align=0
                    )

        result_pdf_base64 = doc_to_base64(doc)
        doc.close()

        return jsonify({
            "success": True,
            "pdf_base64": result_pdf_base64
        })

    except Exception as e:
        return error_response(f"find-replace failed: {str(e)}", 500)


@app.route("/save-text-at-rect", methods=["POST"])
def save_text_at_rect():
    try:
        data = parse_json()
        pdf_base64 = data.get("pdf_base64")
        page_num = int(data.get("page_num", 0))
        new_text = data.get("new_text", "")

        rect_data = data.get("rect")
        if not rect_data:
            return error_response("rect is required")

        x0 = float(rect_data.get("x0"))
        y0 = float(rect_data.get("y0"))
        x1 = float(rect_data.get("x1"))
        y1 = float(rect_data.get("y1"))

        font_size = float(data.get("font_size", 12))
        text_color = data.get("text_color", "#000000")
        align = int(data.get("align", 0))

        color_rgb = tuple(
            int(text_color.lstrip("#")[i:i+2], 16) / 255.0 for i in (0, 2, 4)
        )

        doc = open_pdf_from_base64(pdf_base64)

        if page_num < 0 or page_num >= len(doc):
            doc.close()
            return error_response("Invalid page_num")

        page = doc[page_num]
        rect = fitz.Rect(x0, y0, x1, y1)

        page.add_redact_annot(rect, fill=(1, 1, 1))
        page.apply_redactions()

        page.insert_textbox(
            rect,
            new_text,
            fontsize=font_size,
            color=color_rgb,
            align=align
        )

        result_pdf_base64 = doc_to_base64(doc)
        doc.close()

        return jsonify({
            "success": True,
            "pdf_base64": result_pdf_base64
        })

    except Exception as e:
        return error_response(f"save-text-at-rect failed: {str(e)}", 500)


@app.route("/replace-image", methods=["POST"])
def replace_image():
    try:
        data = parse_json()
        image_base64 = data.get("image_base64")
        page_num = int(data.get("page_num", 0))
        x = float(data.get("x", 0))
        y = float(data.get("y", 0))
        width = float(data.get("width", 100))
        height = float(data.get("height", 100))

        if not image_base64:
            return error_response("image_base64 is required")

        image_bytes = base64.b64decode(image_base64)
        doc = open_pdf_from_base64(data.get("pdf_base64"))

        if page_num < 0 or page_num >= len(doc):
            doc.close()
            return error_response("Invalid page_num")

        page = doc[page_num]
        rect = fitz.Rect(x, y, x + width, y + height)
        page.insert_image(rect, stream=image_bytes)

        result_pdf_base64 = doc_to_base64(doc)
        doc.close()

        return jsonify({
            "success": True,
            "pdf_base64": result_pdf_base64
        })

    except Exception as e:
        return error_response(f"replace-image failed: {str(e)}", 500)


@app.route("/add-annotation", methods=["POST"])
def add_annotation():
    try:
        data = parse_json()
        page_num = int(data.get("page_num", 0))
        annotation_type = data.get("type", "text")
        content = data.get("content", "")
        x = float(data.get("x", 0))
        y = float(data.get("y", 0))

        doc = open_pdf_from_base64(data.get("pdf_base64"))

        if page_num < 0 or page_num >= len(doc):
            doc.close()
            return error_response("Invalid page_num")

        page = doc[page_num]

        if annotation_type == "text":
            page.insert_text(
                (x, y),
                content,
                fontsize=12,
                color=(0, 0, 0)
            )
        else:
            doc.close()
            return error_response("Unsupported annotation type")

        result_pdf_base64 = doc_to_base64(doc)
        doc.close()

        return jsonify({
            "success": True,
            "pdf_base64": result_pdf_base64
        })

    except Exception as e:
        return error_response(f"add-annotation failed: {str(e)}", 500)


@app.route("/add-stamp", methods=["POST"])
def add_stamp():
    try:
        data = parse_json()
        page_num = int(data.get("page_num", 0))
        stamp_text = data.get("stamp_text", "DRAFT")
        x = float(data.get("x", 100))
        y = float(data.get("y", 100))
        color = data.get("color", "#FF0000")
        rotation = int(data.get("rotation", 0))

        if rotation not in [0, 90, 180, 270]:
            return error_response("rotation must be one of 0, 90, 180, 270")

        color_rgb = tuple(
            int(color.lstrip("#")[i:i+2], 16) / 255.0 for i in (0, 2, 4)
        )

        doc = open_pdf_from_base64(data.get("pdf_base64"))

        if page_num < 0 or page_num >= len(doc):
            doc.close()
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
        doc.close()

        return jsonify({
            "success": True,
            "pdf_base64": result_pdf_base64
        })

    except Exception as e:
        return error_response(f"add-stamp failed: {str(e)}", 500)


@app.route("/add-watermark", methods=["POST"])
def add_watermark():
    try:
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
        doc.close()

        return jsonify({
            "success": True,
            "pdf_base64": result_pdf_base64
        })

    except Exception as e:
        return error_response(f"add-watermark failed: {str(e)}", 500)


@app.route("/scan-links", methods=["POST"])
def scan_links():
    try:
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

        doc.close()

        return jsonify({
            "success": True,
            "count": len(all_links),
            "links": all_links
        })

    except Exception as e:
        return error_response(f"scan-links failed: {str(e)}", 500)


@app.route("/replace-links", methods=["POST"])
def replace_links():
    try:
        data = parse_json()
        find_value = data.get("find")
        replace_value = data.get("replace")

        if not find_value:
            return error_response("find is required")
        if replace_value is None:
            return error_response("replace is required")

        doc = open_pdf_from_base64(data.get("pdf_base64"))
        replaced_count = 0

        for page in doc:
            links = page.get_links()
            for link in links:
                uri = link.get("uri")
                rect = link.get("from")

                if uri and find_value in uri and rect:
                    new_uri = uri.replace(find_value, replace_value)

                    try:
                        page.delete_link(link)
                    except Exception:
                        pass

                    page.insert_link({
                        "kind": fitz.LINK_URI,
                        "from": rect,
                        "uri": new_uri
                    })
                    replaced_count += 1

        result_pdf_base64 = doc_to_base64(doc)
        doc.close()

        return jsonify({
            "success": True,
            "replaced_count": replaced_count,
            "pdf_base64": result_pdf_base64
        })

    except Exception as e:
        return error_response(f"replace-links failed: {str(e)}", 500)


# =========================================================
# CAPTURE FORM ROUTES
# =========================================================
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
        subheadline = str(data.get("subheadline", "Enter your details to continue")).strip()
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

        return success_response(data={"data": result}, message="Capture form fetched successfully")

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
            return error_response("No capture form configured for this PDF", 404)

        form_data = dict(form_row)
        provider = form_data.get("provider", "")
        provider_config = json.loads(form_data.get("provider_config_json") or "{}")

        lead_payload = {
            "pdf_id": pdf_id,
            "name": name,
            "email": email,
            "phone": phone,
            "custom_fields": custom_fields,
            "source_url": source_url
        }

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
            "message": "Lead saved locally but provider failed",
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
            data={"count": len(leads), "leads": leads},
            message="Leads fetched successfully"
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
            "name": "Test Lead",
            "email": "test@example.com",
            "phone": "1234567890",
            "custom_fields": {},
            "source_url": "https://example.com/test"
        }

        result = send_lead_to_provider(provider, provider_config, test_lead)
        status_code = result.get("status_code", 500)

        if 200 <= status_code < 300:
            return success_response(data=result, message="Provider connection successful")

        return jsonify({
            "success": False,
            "message": "Provider connection failed",
            **result
        }), 500

    except Exception as e:
        return error_response(f"Provider test failed: {str(e)}", 500)


# =========================================================
# START APP
# =========================================================
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
