from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import json
import os
import requests
from datetime import datetime
import fitz  # PyMuPDF
import base64
import io
import traceback

app = Flask(__name__)
CORS(app)

DB_PATH = os.environ.get("DB_PATH", "database.db")


# -----------------------------
# Database helpers
# -----------------------------
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


# -----------------------------
# Utility helpers
# -----------------------------
def json_response(success=True, message="", data=None, status=200):
return jsonify({
    "success": success,
    "message": message,
    "data": data or {}
}), status


def get_json_body():
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


# -----------------------------
# Provider integrations
# -----------------------------
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


# =============================
# CAPTURE FORM ROUTES
# =============================

@app.route("/", methods=["GET"])
def home():
return jsonify({
    "success": True,
    "message": "PDF Editor Combined API is running",
    "services": {
        "capture_forms": [
            "/capture-form/save",
            "/capture-form/<pdf_id>",
            "/capture-form/submit",
            "/capture-form/leads/<pdf_id>",
            "/capture-form/test-provider"
        ],
        "pdf_editing": [
            "/find-replace",
            "/replace-image",
            "/add-annotation",
            "/add-stamp",
            "/add-watermark"
        ]
    }
})


@app.route("/health", methods=["GET"])
def health():
return jsonify({"success": True, "status": "ok"})


@app.route("/capture-form/save", methods=["POST"])
def save_capture_form():
try:
    data = get_json_body()

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
        return json_response(False, "pdf_id is required", status=400)

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

    return json_response(True, "Capture form saved successfully")

except Exception as e:
    return json_response(False, f"Failed to save capture form: {str(e)}", status=500)


@app.route("/capture-form/<pdf_id>", methods=["GET"])
def get_capture_form(pdf_id):
try:
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM pdf_capture_forms WHERE pdf_id = ?", (pdf_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return json_response(False, "Capture form not found", status=404)

    result = dict(row)
    result["fields"] = json.loads(result.get("fields_json") or "[]")
    result["provider_config"] = json.loads(result.get("provider_config_json") or "{}")
    result.pop("fields_json", None)
    result.pop("provider_config_json", None)

    return json_response(True, "Capture form fetched successfully", result)

except Exception as e:
    return json_response(False, f"Failed to fetch capture form: {str(e)}", status=500)


@app.route("/capture-form/submit", methods=["POST"])
def submit_capture_form():
try:
    data = get_json_body()

    pdf_id = str(data.get("pdf_id", "")).strip()
    name = str(data.get("name", "")).strip()
    email = normalize_email(data.get("email", ""))
    phone = str(data.get("phone", "")).strip()
    custom_fields = data.get("custom_fields", {})
    source_url = str(data.get("source_url", "")).strip()

    if not pdf_id:
        return json_response(False, "pdf_id is required", status=400)

    if not email:
        return json_response(False, "email is required", status=400)

    if not validate_email(email):
        return json_response(False, "Invalid email address", status=400)

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM pdf_capture_forms WHERE pdf_id = ?", (pdf_id,))
    form_row = cur.fetchone()

    if not form_row:
        conn.close()
        return json_response(False, "No capture form configured for this PDF", status=404)

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
        return json_response(True, "Lead submitted successfully", {
            "provider_status": provider_status
        })

    return json_response(False, "Lead saved locally but provider failed", {
        "provider_status": provider_status,
        "provider_response": provider_response
    }, status=500)

except Exception as e:
    return json_response(False, f"Failed to submit lead: {str(e)}", status=500)


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

    return json_response(True, "Leads fetched successfully", {
        "count": len(leads),
        "leads": leads
    })

except Exception as e:
    return json_response(False, f"Failed to fetch leads: {str(e)}", status=500)


@app.route("/capture-form/test-provider", methods=["POST"])
def test_provider():
try:
    data = get_json_body()
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
        return json_response(True, "Provider connection successful", result)

    return json_response(False, "Provider connection failed", result, status=500)

except Exception as e:
    return json_response(False, f"Provider test failed: {str(e)}", status=500)


# =============================
# PYMUPDF PDF EDITING ROUTES
# =============================

@app.route('/find-replace', methods=['POST'])
def find_replace():
try:
    data = request.json
    pdf_base64 = data.get('pdf_base64')
    replacements = data.get('replacements', [])
    
    if not pdf_base64:
        return jsonify({"error": "Missing pdf_base64"}), 400
    
    # Decode PDF
    pdf_bytes = base64.b64decode(pdf_base64)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    
    # Apply replacements
    for repl in replacements:
        old_text = repl.get('find', '')
        new_text = repl.get('replace', '')
        
        if not old_text:
            continue
        
        # Search and replace across all pages
        for page in doc:
            text_instances = page.search_for(old_text)
            for inst in text_instances:
                page.add_redact_annot(inst, text=new_text, fill=(1, 1, 1))
            page.apply_redactions()
    
    # Save to bytes
    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    
    # Encode to base64
    result_base64 = base64.b64encode(output.read()).decode('utf-8')
    
    return jsonify({
        "success": True,
        "pdf_base64": result_base64
    })
    
except Exception as e:
    traceback.print_exc()
    return jsonify({"error": str(e)}), 500


@app.route('/replace-image', methods=['POST'])
def replace_image():
try:
    data = request.json
    pdf_base64 = data.get('pdf_base64')
    page_num = data.get('page_num', 0)
    image_base64 = data.get('image_base64')
    x = data.get('x', 0)
    y = data.get('y', 0)
    width = data.get('width', 100)
    height = data.get('height', 100)
    
    if not pdf_base64 or not image_base64:
        return jsonify({"error": "Missing pdf_base64 or image_base64"}), 400
    
    # Decode PDF
    pdf_bytes = base64.b64decode(pdf_base64)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    
    # Decode image
    image_bytes = base64.b64decode(image_base64)
    
    # Get page
    page = doc[page_num]
    
    # Insert image
    rect = fitz.Rect(x, y, x + width, y + height)
    page.insert_image(rect, stream=image_bytes)
    
    # Save to bytes
    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    
    # Encode to base64
    result_base64 = base64.b64encode(output.read()).decode('utf-8')
    
    return jsonify({
        "success": True,
        "pdf_base64": result_base64
    })
    
except Exception as e:
    traceback.print_exc()
    return jsonify({"error": str(e)}), 500


@app.route('/add-annotation', methods=['POST'])
def add_annotation():
try:
    data = request.json
    pdf_base64 = data.get('pdf_base64')
    page_num = data.get('page_num', 0)
    annotation_type = data.get('type', 'text')
    x = data.get('x', 0)
    y = data.get('y', 0)
    width = data.get('width', 100)
    height = data.get('height', 50)
    text = data.get('text', '')
    color = data.get('color', [1, 1, 0])  # RGB
    
    if not pdf_base64:
        return jsonify({"error": "Missing pdf_base64"}), 400
    
    # Decode PDF
    pdf_bytes = base64.b64decode(pdf_base64)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    
    # Get page
    page = doc[page_num]
    
    # Create annotation based on type
    rect = fitz.Rect(x, y, x + width, y + height)
    
    if annotation_type == 'highlight':
        annot = page.add_highlight_annot(rect)
    elif annotation_type == 'underline':
        annot = page.add_underline_annot(rect)
    elif annotation_type == 'strikeout':
        annot = page.add_strikeout_annot(rect)
    elif annotation_type == 'square':
        annot = page.add_square_annot(rect)
    elif annotation_type == 'circle':
        annot = page.add_circle_annot(rect)
    else:  # text annotation
        annot = page.add_text_annot(fitz.Point(x, y), text)
    
    if hasattr(annot, 'set_colors'):
        annot.set_colors(stroke=color)
    annot.update()
    
    # Save to bytes
    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    
    # Encode to base64
    result_base64 = base64.b64encode(output.read()).decode('utf-8')
    
    return jsonify({
        "success": True,
        "pdf_base64": result_base64
    })
    
except Exception as e:
    traceback.print_exc()
    return jsonify({"error": str(e)}), 500


@app.route('/add-stamp', methods=['POST'])
def add_stamp():
try:
    data = request.json
    pdf_base64 = data.get('pdf_base64')
    page_num = data.get('page_num', 0)
    stamp_text = data.get('stamp_text', 'APPROVED')
    x = data.get('x', 100)
    y = data.get('y', 100)
    color = data.get('color', '#FF0000')
    rotation = data.get('rotation', 0)
    font_size = data.get('font_size', 48)
    
    if not pdf_base64:
        return jsonify({"error": "Missing pdf_base64"}), 400
    
    # Decode PDF
    pdf_bytes = base64.b64decode(pdf_base64)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    
    # Get page
    page = doc[page_num]
    
    # Convert hex color to RGB
    color_hex = color.lstrip('#')
    r = int(color_hex[0:2], 16) / 255
    g = int(color_hex[2:4], 16) / 255
    b = int(color_hex[4:6], 16) / 255
    
    # Insert text as stamp
    point = fitz.Point(x, y)
    text_writer = fitz.TextWriter(page.rect)
    text_writer.append(
        point,
        stamp_text,
        fontsize=font_size,
        color=(r, g, b)
    )
    
    if rotation != 0:
        text_writer.rotate = rotation
    
    text_writer.write_text(page)
    
    # Save to bytes
    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    
    # Encode to base64
    result_base64 = base64.b64encode(output.read()).decode('utf-8')
    
    return jsonify({
        "success": True,
        "pdf_base64": result_base64
    })
    
except Exception as e:
    traceback.print_exc()
    return jsonify({"error": str(e)}), 500


@app.route('/add-watermark', methods=['POST'])
def add_watermark():
try:
    data = request.json
    pdf_base64 = data.get('pdf_base64')
    watermark_text = data.get('watermark_text', 'CONFIDENTIAL')
    opacity = data.get('opacity', 0.3)
    color = data.get('color', '#000000')
    font_size = data.get('font_size', 72)
    rotation = data.get('rotation', 45)
    
    if not pdf_base64:
        return jsonify({"error": "Missing pdf_base64"}), 400
    
    # Decode PDF
    pdf_bytes = base64.b64decode(pdf_base64)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    
    # Convert hex color to RGB
    color_hex = color.lstrip('#')
    r = int(color_hex[0:2], 16) / 255
    g = int(color_hex[2:4], 16) / 255
    b = int(color_hex[4:6], 16) / 255
    
    # Add watermark to all pages
    for page in doc:
        # Calculate center position
        rect = page.rect
        center_x = rect.width / 2
        center_y = rect.height / 2
        
        # Create watermark
        text_writer = fitz.TextWriter(page.rect, opacity=opacity)
        text_writer.append(
            fitz.Point(center_x, center_y),
            watermark_text,
            fontsize=font_size,
            color=(r, g, b)
        )
        
        if rotation != 0:
            text_writer.rotate = rotation
        
        text_writer.write_text(page)
    
    # Save to bytes
    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    
    # Encode to base64
    result_base64 = base64.b64encode(output.read()).decode('utf-8')
    
    return jsonify({
        "success": True,
        "pdf_base64": result_base64
    })
    
except Exception as e:
    traceback.print_exc()
    return jsonify({"error": str(e)}), 500


# -----------------------------
# App start
# -----------------------------
if __name__ == "__main__":
init_db()
port = int(os.environ.get("PORT", 5000))
app.run(host="0.0.0.0", port=port)
else:
init_db()
