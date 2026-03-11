from flask import Flask, request, jsonify
from flask_cors import CORS
import fitz  # PyMuPDF
import base64
import io
import os

app = Flask(__name__)
CORS(app)


def success_pdf_response(doc):
    output = io.BytesIO()
    doc.save(output, garbage=4, deflate=True)
    doc.close()
    output.seek(0)
    result_base64 = base64.b64encode(output.read()).decode("utf-8")
    return jsonify({"pdf_base64": result_base64})


def error_response(message, status=400):
    return jsonify({"error": message}), status


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/find-replace", methods=["POST"])
def find_replace():
    try:
        data = request.get_json(force=True)
        pdf_base64 = data.get("pdf_base64")
        replacements = data.get("replacements", [])

        if not pdf_base64:
            return error_response("pdf_base64 is required")

        pdf_bytes = base64.b64decode(pdf_base64)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        for page in doc:
            for replacement in replacements:
                old_text = replacement.get("old_text")
                new_text = replacement.get("new_text")

                if not old_text or new_text is None:
                    continue

                text_instances = page.search_for(old_text)

                for rect in text_instances:
                    # Cover old text with white redaction area
                    page.add_redact_annot(rect, fill=(1, 1, 1))

                if text_instances:
                    page.apply_redactions()

                    # Reinsert replacement text into same area
                    for rect in text_instances:
                        page.insert_textbox(
                            rect,
                            new_text,
                            fontsize=12,
                            color=(0, 0, 0),
                            align=0
                        )

        return success_pdf_response(doc)

    except Exception as e:
        return error_response(f"find-replace failed: {str(e)}", 500)


@app.route("/replace-image", methods=["POST"])
def replace_image():
    try:
        data = request.get_json(force=True)
        pdf_base64 = data.get("pdf_base64")
        page_num = int(data.get("page_num", 0))
        image_base64 = data.get("image_base64")
        x = float(data.get("x", 0))
        y = float(data.get("y", 0))
        width = float(data.get("width", 100))
        height = float(data.get("height", 100))

        if not pdf_base64:
            return error_response("pdf_base64 is required")
        if not image_base64:
            return error_response("image_base64 is required")

        pdf_bytes = base64.b64decode(pdf_base64)
        image_bytes = base64.b64decode(image_base64)

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        if page_num < 0 or page_num >= len(doc):
            doc.close()
            return error_response("Invalid page_num")

        page = doc[page_num]
        rect = fitz.Rect(x, y, x + width, y + height)
        page.insert_image(rect, stream=image_bytes)

        return success_pdf_response(doc)

    except Exception as e:
        return error_response(f"replace-image failed: {str(e)}", 500)


@app.route("/add-annotation", methods=["POST"])
def add_annotation():
    try:
        data = request.get_json(force=True)
        pdf_base64 = data.get("pdf_base64")
        page_num = int(data.get("page_num", 0))
        annotation_type = data.get("type", "text")
        content = data.get("content", "")
        x = float(data.get("x", 0))
        y = float(data.get("y", 0))

        if not pdf_base64:
            return error_response("pdf_base64 is required")

        pdf_bytes = base64.b64decode(pdf_base64)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

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

        return success_pdf_response(doc)

    except Exception as e:
        return error_response(f"add-annotation failed: {str(e)}", 500)


@app.route("/add-stamp", methods=["POST"])
def add_stamp():
    try:
        data = request.get_json(force=True)
        pdf_base64 = data.get("pdf_base64")
        page_num = int(data.get("page_num", 0))
        stamp_text = data.get("stamp_text", "DRAFT")
        x = float(data.get("x", 100))
        y = float(data.get("y", 100))
        color = data.get("color", "#FF0000")
        rotation = int(data.get("rotation", 0))

        if not pdf_base64:
            return error_response("pdf_base64 is required")

        if rotation not in [0, 90, 180, 270]:
            return error_response("rotation must be one of: 0, 90, 180, 270")

        pdf_bytes = base64.b64decode(pdf_base64)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        if page_num < 0 or page_num >= len(doc):
            doc.close()
            return error_response("Invalid page_num")

        page = doc[page_num]

        color_rgb = tuple(
            int(color.lstrip("#")[i:i+2], 16) / 255.0 for i in (0, 2, 4)
        )

        page.insert_text(
            (x, y),
            stamp_text,
            fontsize=36,
            color=color_rgb,
            rotate=rotation
        )

        return success_pdf_response(doc)

    except Exception as e:
        return error_response(f"add-stamp failed: {str(e)}", 500)


@app.route("/add-watermark", methods=["POST"])
def add_watermark():
    try:
        data = request.get_json(force=True)
        pdf_base64 = data.get("pdf_base64")
        watermark_text = data.get("text", "CONFIDENTIAL")

        if not pdf_base64:
            return error_response("pdf_base64 is required")

        pdf_bytes = base64.b64decode(pdf_base64)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        for page in doc:
            rect = page.rect

            watermark_box = fitz.Rect(
                rect.width * 0.15,
                rect.height * 0.40,
                rect.width * 0.85,
                rect.height * 0.60
            )

            page.insert_textbox(
                watermark_box,
                watermark_text,
                fontsize=40,
                color=(0.7, 0.7, 0.7),
                align=1,
                rotate=0
            )

        return success_pdf_response(doc)

    except Exception as e:
        return error_response(f"add-watermark failed: {str(e)}", 500)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
