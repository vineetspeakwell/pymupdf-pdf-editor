from flask import Flask, request, jsonify
from flask_cors import CORS
import fitz  # PyMuPDF
import base64
import io

app = Flask(__name__)
CORS(app)

@app.route('/health', methods=['GET'])
def health():
return jsonify({"status": "ok"})

@app.route('/find-replace', methods=['POST'])
def find_replace():
data = request.json
pdf_base64 = data.get('pdf_base64')
replacements = data.get('replacements', [])

# Decode PDF
pdf_bytes = base64.b64decode(pdf_base64)
doc = fitz.open(stream=pdf_bytes, filetype="pdf")

# Perform replacements
for page in doc:
    for replacement in replacements:
        old_text = replacement.get('old_text')
        new_text = replacement.get('new_text')
        if old_text and new_text:
            page.replace_text(old_text, new_text)

# Save and return
output = io.BytesIO()
doc.save(output)
doc.close()
output.seek(0)

result_base64 = base64.b64encode(output.read()).decode('utf-8')
return jsonify({"pdf_base64": result_base64})

@app.route('/replace-image', methods=['POST'])
def replace_image():
data = request.json
pdf_base64 = data.get('pdf_base64')
page_num = data.get('page_num', 0)
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

# Save and return
output = io.BytesIO()
doc.save(output)
doc.close()
output.seek(0)

result_base64 = base64.b64encode(output.read()).decode('utf-8')
return jsonify({"pdf_base64": result_base64})

@app.route('/add-annotation', methods=['POST'])
def add_annotation():
data = request.json
pdf_base64 = data.get('pdf_base64')
page_num = data.get('page_num', 0)
annotation_type = data.get('type', 'text')
content = data.get('content', '')
x = data.get('x', 0)
y = data.get('y', 0)

pdf_bytes = base64.b64decode(pdf_base64)
doc = fitz.open(stream=pdf_bytes, filetype="pdf")
page = doc[page_num]

# Add annotation
if annotation_type == 'text':
    page.insert_text((x, y), content, fontsize=12, color=(0, 0, 0))

output = io.BytesIO()
doc.save(output)
doc.close()
output.seek(0)

result_base64 = base64.b64encode(output.read()).decode('utf-8')
return jsonify({"pdf_base64": result_base64})

@app.route('/add-stamp', methods=['POST'])
def add_stamp():
data = request.json
pdf_base64 = data.get('pdf_base64')
page_num = data.get('page_num', 0)
stamp_text = data.get('stamp_text', 'DRAFT')
x = data.get('x', 100)
y = data.get('y', 100)
color = data.get('color', '#FF0000')
rotation = data.get('rotation', 0)

pdf_bytes = base64.b64decode(pdf_base64)
doc = fitz.open(stream=pdf_bytes, filetype="pdf")
page = doc[page_num]

# Convert hex color to RGB
color_rgb = tuple(int(color.lstrip('#')[i:i+2], 16) / 255 for i in (0, 2, 4))

# Add stamp text
page.insert_text((x, y), stamp_text, fontsize=48, color=color_rgb, rotate=rotation)

output = io.BytesIO()
doc.save(output)
doc.close()
output.seek(0)

result_base64 = base64.b64encode(output.read()).decode('utf-8')
return jsonify({"pdf_base64": result_base64})

@app.route('/add-watermark', methods=['POST'])
def add_watermark():
data = request.json
pdf_base64 = data.get('pdf_base64')
watermark_text = data.get('text', 'CONFIDENTIAL')
opacity = data.get('opacity', 0.3)

pdf_bytes = base64.b64decode(pdf_base64)
doc = fitz.open(stream=pdf_bytes, filetype="pdf")

# Add watermark to all pages
for page in doc:
    page_width = page.rect.width
    page_height = page.rect.height
    x = page_width / 2
    y = page_height / 2
    
    page.insert_text((x, y), watermark_text, fontsize=72, 
                    color=(0.5, 0.5, 0.5), rotate=45)

output = io.BytesIO()
doc.save(output)
doc.close()
output.seek(0)

result_base64 = base64.b64encode(output.read()).decode('utf-8')
return jsonify({"pdf_base64": result_base64})

if __name__ == '__main__':
app.run(host='0.0.0.0', port=5000)
