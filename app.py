from flask import Flask, request, jsonify, send_file
import fitz  # PyMuPDF
import base64
import io
import requests
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

@app.route('/health', methods=['GET'])
def health():
return jsonify({"status": "ok"})

@app.route('/find-replace', methods=['POST'])
def find_replace():
try:
    data = request.json
    pdf_base64 = data.get('pdf_base64')
    replacements = data.get('replacements', [])
    
    # Decode PDF
    pdf_bytes = base64.b64decode(pdf_base64)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    
    # Apply replacements
    for replacement in replacements:
        old_text = replacement.get('old_text')
        new_text = replacement.get('new_text')
        
        for page in doc:
            text_instances = page.search_for(old_text)
            for inst in text_instances:
                page.add_redact_annot(inst, text=new_text, fill=(1, 1, 1))
            page.apply_redactions()
    
    # Return modified PDF as base64
    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    
    result_base64 = base64.b64encode(output.read()).decode('utf-8')
    return jsonify({"pdf_base64": result_base64})
    
except Exception as e:
    return jsonify({"error": str(e)}), 500

@app.route('/replace-image', methods=['POST'])
def replace_image():
try:
    data = request.json
    pdf_base64 = data.get('pdf_base64')
    page_num = data.get('page_number', 0)
    image_base64 = data.get('image_base64')
    x = data.get('x', 0)
    y = data.get('y', 0)
    width = data.get('width', 100)
    height = data.get('height', 100)
    
    # Decode PDF and image
    pdf_bytes = base64.b64decode(pdf_base64)
    image_bytes = base64.b64decode(image_base64)
    
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_num]
    
    # Insert image
    rect = fitz.Rect(x, y, x + width, y + height)
    page.insert_image(rect, stream=image_bytes)
    
    # Return modified PDF
    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    
    result_base64 = base64.b64encode(output.read()).decode('utf-8')
    return jsonify({"pdf_base64": result_base64})
    
except Exception as e:
    return jsonify({"error": str(e)}), 500

@app.route('/add-annotation', methods=['POST'])
def add_annotation():
try:
    data = request.json
    pdf_base64 = data.get('pdf_base64')
    page_num = data.get('page_number', 0)
    annotation_type = data.get('type', 'text')
    text = data.get('text', '')
    x = data.get('x', 0)
    y = data.get('y', 0)
    width = data.get('width', 100)
    height = data.get('height', 20)
    color = data.get('color', [1, 0, 0])
    
    pdf_bytes = base64.b64decode(pdf_base64)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_num]
    
    rect = fitz.Rect(x, y, x + width, y + height)
    
    if annotation_type == 'text':
        page.insert_textbox(rect, text, fontsize=12, color=color)
    elif annotation_type == 'rectangle':
        page.draw_rect(rect, color=color, width=2)
    elif annotation_type == 'highlight':
        highlight = page.add_highlight_annot(rect)
        highlight.set_colors(stroke=color)
        highlight.update()
    
    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    
    result_base64 = base64.b64encode(output.read()).decode('utf-8')
    return jsonify({"pdf_base64": result_base64})
    
except Exception as e:
    return jsonify({"error": str(e)}), 500

@app.route('/add-stamp', methods=['POST'])
def add_stamp():
try:
    data = request.json
    pdf_base64 = data.get('pdf_base64')
    page_num = data.get('page_number', 0)
    text = data.get('text', 'DRAFT')
    x = data.get('x', 100)
    y = data.get('y', 100)
    rotation = data.get('rotation', 0)
    color = data.get('color', [1, 0, 0])
    
    pdf_bytes = base64.b64decode(pdf_base64)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_num]
    
    # Add rotated text stamp
    rect = fitz.Rect(x, y, x + 200, y + 50)
    page.insert_textbox(rect, text, fontsize=36, color=color, rotate=rotation)
    
    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    
    result_base64 = base64.b64encode(output.read()).decode('utf-8')
    return jsonify({"pdf_base64": result_base64})
    
except Exception as e:
    return jsonify({"error": str(e)}), 500

@app.route('/add-watermark', methods=['POST'])
def add_watermark():
try:
    data = request.json
    pdf_base64 = data.get('pdf_base64')
    text = data.get('text', 'CONFIDENTIAL')
    opacity = data.get('opacity', 0.3)
    
    pdf_bytes = base64.b64decode(pdf_base64)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    
    for page in doc:
        rect = page.rect
        page.insert_textbox(
            rect,
            text,
            fontsize=60,
            color=[0.5, 0.5, 0.5],
            rotate=45,
            align=fitz.TEXT_ALIGN_CENTER
        )
    
    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    
    result_base64 = base64.b64encode(output.read()).decode('utf-8')
    return jsonify({"pdf_base64": result_base64})
    
except Exception as e:
    return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
app.run(host='0.0.0.0', port=5000)