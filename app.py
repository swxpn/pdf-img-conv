import os
import re
import uuid
import zipfile
import tempfile
import threading
import time
from datetime import datetime, timezone

import fitz  # PyMuPDF
from PIL import Image
from flask import Flask, request, jsonify, send_file, render_template_string
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, PyMongoError

app = Flask(__name__)


def ensure_mongo_schema(database):
    validator = {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["type", "session_id", "created_at"],
            "properties": {
                "type": {
                    "enum": ["pdf_to_image", "image_to_pdf"],
                },
                "session_id": {
                    "bsonType": "string",
                    "minLength": 8,
                },
                "created_at": {
                    "bsonType": "date",
                },
                "format": {
                    "enum": ["PNG", "JPEG"],
                },
                "dpi": {
                    "enum": [72, 150, 300],
                },
                "pages_converted": {
                    "bsonType": ["int", "long", "double"],
                },
                "total_pages": {
                    "bsonType": ["int", "long", "double"],
                },
                "page_size": {
                    "enum": ["A4", "LETTER", "A3"],
                },
                "orientation": {
                    "enum": ["portrait", "landscape"],
                },
                "image_count": {
                    "bsonType": ["int", "long", "double"],
                },
            },
            "oneOf": [
                {
                    "properties": {"type": {"enum": ["pdf_to_image"]}},
                    "required": ["format", "dpi", "pages_converted", "total_pages"],
                },
                {
                    "properties": {"type": {"enum": ["image_to_pdf"]}},
                    "required": ["page_size", "orientation", "image_count"],
                },
            ],
        }
    }

    if "conversions" in database.list_collection_names():
        database.command(
            {
                "collMod": "conversions",
                "validator": validator,
                "validationLevel": "strict",
                "validationAction": "error",
            }
        )
    else:
        database.create_collection(
            "conversions",
            validator=validator,
            validationLevel="strict",
            validationAction="error",
        )


def ensure_mongo_indexes(database):
    conversions = database.conversions
    conversions.create_index("session_id", unique=True, name="uniq_session_id")
    conversions.create_index([("created_at", -1)], name="created_at_desc")
    conversions.create_index(
        [("type", 1), ("created_at", -1)],
        name="type_created_at_desc",
    )


# MongoDB Atlas
MONGO_URI = os.environ.get("MONGO_URI") or os.environ.get("MONGODB_URI", "")
db = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command("ping")
        db = mongo_client.get_default_database("pdf_img_converter")
        ensure_mongo_schema(db)
        ensure_mongo_indexes(db)
        print("Connected to MongoDB Atlas successfully.")
    except ConnectionFailure as e:
        print(f"MongoDB connection failed: {e}")
        db = None
    except PyMongoError as e:
        print(f"MongoDB setup failed: {e}")
        db = None
else:
    print("MONGODB_URI not set - running without database.")

# Temp session store: session_id -> {"dir": path, "images": [...], "zip": path}
SESSIONS: dict = {}
SESSION_TTL = 600  # seconds - clean up after 10 minutes


# Helpers

def parse_page_range(page_range_str: str, total_pages: int) -> list:
    s = page_range_str.strip().lower()
    if s == "" or s == "all":
        return list(range(total_pages))
    indices = set()
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
            if not m:
                raise ValueError(f"Invalid range: '{part}'")
            start, end = int(m.group(1)), int(m.group(2))
            if start < 1 or end > total_pages or start > end:
                raise ValueError(
                    f"Range '{part}' out of bounds (doc has {total_pages} pages)"
                )
            indices.update(range(start - 1, end))
        else:
            if not part.isdigit():
                raise ValueError(f"Invalid page: '{part}'")
            n = int(part)
            if n < 1 or n > total_pages:
                raise ValueError(
                    f"Page {n} out of bounds (doc has {total_pages} pages)"
                )
            indices.add(n - 1)
    return sorted(indices)


def cleanup_old_sessions():
    while True:
        time.sleep(60)
        now = time.time()
        expired = [
            sid
            for sid, meta in list(SESSIONS.items())
            if now - meta.get("created", now) > SESSION_TTL
        ]
        for sid in expired:
            meta = SESSIONS.pop(sid, {})
            d = meta.get("dir")
            if d and os.path.isdir(d):
                import shutil

                shutil.rmtree(d, ignore_errors=True)


threading.Thread(target=cleanup_old_sessions, daemon=True).start()


# Routes

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/convert", methods=["POST"])
def convert():
    pdf_file = request.files.get("pdf")
    fmt = request.form.get("format", "PNG").upper()
    dpi = int(request.form.get("dpi", 150))
    page_range_str = request.form.get("pages", "all")

    if not pdf_file or pdf_file.filename == "":
        return jsonify({"error": "No PDF file provided."}), 400
    if fmt not in ("PNG", "JPEG"):
        return jsonify({"error": "Format must be PNG or JPEG."}), 400
    if dpi not in (72, 150, 300):
        return jsonify({"error": "DPI must be 72, 150, or 300."}), 400

    session_id = uuid.uuid4().hex
    tmp_dir = tempfile.mkdtemp(prefix=f"pdf2img_{session_id}_")

    try:
        pdf_path = os.path.join(tmp_dir, "input.pdf")
        pdf_file.save(pdf_path)

        doc = fitz.open(pdf_path)
        total = len(doc)

        pages = parse_page_range(page_range_str, total)
        if not pages:
            return jsonify({"error": "No pages selected."}), 400

        scale = dpi / 72.0
        mat = fitz.Matrix(scale, scale)
        ext = "jpg" if fmt == "JPEG" else "png"
        image_names = []

        for idx in pages:
            page = doc[idx]
            pix = page.get_pixmap(matrix=mat, alpha=False)
            name = f"page_{idx + 1:04d}.{ext}"
            img_path = os.path.join(tmp_dir, name)
            if fmt == "JPEG":
                pix.save(img_path, jpg_quality=92)
            else:
                pix.save(img_path)
            image_names.append(name)

        doc.close()

        zip_name = "converted_images.zip"
        zip_path = os.path.join(tmp_dir, zip_name)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in image_names:
                zf.write(os.path.join(tmp_dir, name), arcname=name)

        SESSIONS[session_id] = {
            "dir": tmp_dir,
            "images": image_names,
            "zip": zip_name,
            "created": time.time(),
        }

        if db is not None:
            db.conversions.insert_one(
                {
                    "type": "pdf_to_image",
                    "session_id": session_id,
                    "format": fmt,
                    "dpi": dpi,
                    "pages_converted": len(pages),
                    "total_pages": total,
                    "created_at": datetime.now(timezone.utc),
                }
            )

        return jsonify(
            {
                "session": session_id,
                "count": len(pages),
                "images": [f"/file/{session_id}/{n}" for n in image_names],
            "zip": f"/download/{session_id}/{zip_name}",
            }
        )

    except ValueError as e:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": f"Conversion failed: {e}"}), 500


ALLOWED_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tiff",
    ".tif",
    ".gif",
    ".webp",
}


@app.route("/img2pdf", methods=["POST"])
def img_to_pdf():
    files = request.files.getlist("images")
    page_size = request.form.get("page_size", "A4").upper()
    orientation = request.form.get("orientation", "portrait").lower()

    if not files or all(f.filename == "" for f in files):
        return jsonify({"error": "No image files provided."}), 400

    sizes = {
        "A4": (595.28, 841.89),
        "LETTER": (612, 792),
        "A3": (841.89, 1190.55),
    }
    if page_size not in sizes:
        return jsonify({"error": "Page size must be A4, Letter, or A3."}), 400
    if orientation not in ("portrait", "landscape"):
        return jsonify({"error": "Orientation must be portrait or landscape."}), 400

    w, h = sizes[page_size]
    if orientation == "landscape":
        w, h = h, w

    session_id = uuid.uuid4().hex
    tmp_dir = tempfile.mkdtemp(prefix=f"img2pdf_{session_id}_")

    try:
        doc = fitz.open()
        for f in files:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in ALLOWED_IMAGE_EXTENSIONS:
                continue
            img_path = os.path.join(tmp_dir, uuid.uuid4().hex + ext)
            f.save(img_path)

            img = Image.open(img_path)
            img_w, img_h = img.size
            img.close()

            page = doc.new_page(width=w, height=h)
            iw, ih = float(img_w), float(img_h)
            scale = min(w / iw, h / ih)
            rw, rh = iw * scale, ih * scale
            x0 = (w - rw) / 2
            y0 = (h - rh) / 2
            rect = fitz.Rect(x0, y0, x0 + rw, y0 + rh)
            page.insert_image(rect, filename=img_path)

        if len(doc) == 0:
            return jsonify({"error": "No valid image files found."}), 400

        first_name = next(
            (
                f.filename
                for f in files
                if os.path.splitext(f.filename)[1].lower() in ALLOWED_IMAGE_EXTENSIONS
            ),
            "images",
        )
        base = os.path.splitext(os.path.basename(first_name))[0]
        pdf_name = f"converted-{base}.pdf"
        pdf_path = os.path.join(tmp_dir, pdf_name)
        doc.save(pdf_path)
        doc.close()

        SESSIONS[session_id] = {
            "dir": tmp_dir,
            "pdf": pdf_name,
            "created": time.time(),
        }

        if db is not None:
            db.conversions.insert_one(
                {
                    "type": "image_to_pdf",
                    "session_id": session_id,
                    "page_size": page_size,
                    "orientation": orientation,
                    "image_count": len(files),
                    "created_at": datetime.now(timezone.utc),
                }
            )

        return jsonify(
            {
                "session": session_id,
                "pages": len(files),
            "pdf": f"/download/{session_id}/{pdf_name}",
            }
        )

    except Exception as e:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": f"Conversion failed: {e}"}), 500


@app.route("/file/<session_id>/<filename>")
def serve_file(session_id, filename):
    meta = SESSIONS.get(session_id)
    if not meta:
        return "Session not found or expired.", 404
    safe_name = os.path.basename(filename)
    path = os.path.join(meta["dir"], safe_name)
    if not os.path.isfile(path):
        return "File not found.", 404
    as_attachment = safe_name.endswith(".zip") or safe_name.endswith(".pdf")
    return send_file(
        path,
        as_attachment=as_attachment,
        download_name=safe_name if as_attachment else None,
    )


@app.route("/download/<session_id>/<filename>")
def download_file(session_id, filename):
    meta = SESSIONS.get(session_id)
    if not meta:
        return "Session not found or expired.", 404
    safe_name = os.path.basename(filename)
    path = os.path.join(meta["dir"], safe_name)
    if not os.path.isfile(path):
        return "File not found.", 404
    return send_file(path, as_attachment=True, download_name=safe_name)


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PDF &amp; Image Converter</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --sw-orange: #fc8019;
    --sw-orange-deep: #e86c00;
    --sw-ink: #1f1f24;
    --sw-muted: #63636f;
    --sw-bg: #fff7f1;
    --sw-card: #ffffff;
    --sw-line: #f0dfd2;
    --sw-green: #27ae60;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: "Poppins", sans-serif;
    color: var(--sw-ink);
    min-height: 100vh;
    background:
      radial-gradient(circle at 85% -10%, #ffd8bd 0%, rgba(255, 216, 189, 0) 40%),
      radial-gradient(circle at 10% 0%, #ffe8d7 0%, rgba(255, 232, 215, 0) 42%),
      var(--sw-bg);
  }

  .container {
    max-width: 1140px;
    margin: 0 auto;
    padding: 0 1rem;
  }

  .hero {
    padding: 1.25rem 0 1.1rem;
  }

  .topbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    margin-bottom: 1rem;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: .7rem;
    font-weight: 800;
    letter-spacing: .2px;
  }

  .brand-badge {
    width: 34px;
    height: 34px;
    border-radius: 10px;
    background: linear-gradient(135deg, var(--sw-orange), var(--sw-orange-deep));
    display: grid;
    place-items: center;
    color: #fff;
    font-size: 1rem;
    font-weight: 800;
    box-shadow: 0 10px 22px rgba(252, 128, 25, .33);
  }

  .top-note {
    color: var(--sw-muted);
    font-size: .83rem;
    font-weight: 500;
  }

  .hero-card {
    background: linear-gradient(120deg, #fff 0%, #fff6ef 100%);
    border: 1px solid #ffe0c7;
    border-radius: 24px;
    padding: 1.4rem 1.25rem;
    box-shadow: 0 18px 38px rgba(252, 128, 25, .13);
    animation: rise .45s ease-out;
  }

  .hero-grid {
    display: grid;
    grid-template-columns: 1.2fr .8fr;
    gap: 1rem;
    align-items: center;
  }

  .hero h1 {
    font-size: clamp(1.4rem, 2.6vw, 2.2rem);
    line-height: 1.15;
    margin-bottom: .35rem;
  }

  .hero p {
    color: var(--sw-muted);
    font-size: .92rem;
    max-width: 54ch;
  }

  .pill-row {
    display: flex;
    flex-wrap: wrap;
    gap: .45rem;
    justify-content: flex-end;
  }

  .pill {
    border: 1px solid #ffd9bc;
    background: #fff;
    color: #7f4a1d;
    padding: .45rem .68rem;
    border-radius: 999px;
    font-size: .78rem;
    font-weight: 600;
  }

  .tabs {
    display: flex;
    gap: .6rem;
    max-width: 1140px;
    margin: .7rem auto 0;
    padding: 0 1rem;
  }
  .tab-btn {
    padding: .66rem 1.1rem;
    background: #fff;
    border: 1px solid var(--sw-line);
    cursor: pointer;
    font-size: 0.86rem;
    font-weight: 700;
    color: #6c6c74;
    border-radius: 999px;
    transition: all .15s;
  }
  .tab-btn.active {
    background: linear-gradient(135deg, var(--sw-orange), var(--sw-orange-deep));
    color: #fff;
    border-color: transparent;
    box-shadow: 0 11px 24px rgba(252, 128, 25, .36);
  }

  .tab-content { display: none; }
  .tab-content.active { display: grid; }

  main {
    max-width: 1140px;
    margin: 0 auto;
    padding: .8rem 1rem 2.2rem;
    display: grid;
    grid-template-columns: 360px 1fr;
    gap: 1rem;
  }

  .panel {
    background: var(--sw-card);
    border-radius: 18px;
    padding: 1.15rem;
    border: 1px solid #f4e0d1;
    box-shadow: 0 10px 30px rgba(31, 31, 36, .06);
    animation: rise .55s ease-out;
  }

  .panel h2 {
    font-size: .98rem;
    font-weight: 700;
    margin-bottom: .95rem;
  }

  label {
    display: block;
    font-size: 0.76rem;
    font-weight: 700;
    margin-bottom: 0.34rem;
    color: #595965;
    text-transform: uppercase;
    letter-spacing: .35px;
  }

  .field { margin-bottom: .85rem; }

  input[type=text], select {
    width: 100%;
    padding: .66rem .72rem;
    border: 1px solid #efddcf;
    border-radius: 11px;
    font-size: 0.9rem;
    outline: none;
    transition: border .15s, box-shadow .15s;
    background: #fff;
  }

  input[type=text]:focus, select:focus {
    border-color: #ffc799;
    box-shadow: 0 0 0 4px rgba(252, 128, 25, .13);
  }

  .radio-group { display: flex; gap: .6rem; }
  .radio-group label {
    flex: 1;
    text-align: center;
    padding: .5rem;
    border: 1px solid #efddcf;
    border-radius: 11px;
    cursor: pointer;
    font-size: 0.83rem;
    font-weight: 600;
    transition: all .15s;
    color: #444;
    text-transform: none;
    letter-spacing: 0;
    margin-bottom: 0;
  }

  .radio-group input { display: none; }
  .radio-group input:checked + label {
    background: #fff4eb;
    color: #7d3f13;
    border-color: #ffc799;
  }

  .drop-zone {
    border: 2px dashed #ffcda2;
    border-radius: 14px;
    padding: 1.4rem .75rem;
    text-align: center;
    cursor: pointer;
    transition: all .18s;
    margin-bottom: .75rem;
    background: linear-gradient(180deg, #fff9f4 0%, #fff 100%);
  }

  .drop-zone:hover { transform: translateY(-2px); }
  .drop-zone.dragover { border-color: var(--sw-orange); background: #fff2e7; }
  .drop-zone input { display: none; }

  .drop-zone p { font-size: 0.84rem; color: #807b75; margin-top: 0.48rem; }
  .drop-zone .filename { font-size: 0.84rem; color: #6e360f; font-weight: 600; margin-top: .46rem; }

  .btn {
    display: block;
    width: 100%;
    padding: .74rem;
    background: linear-gradient(135deg, var(--sw-orange), var(--sw-orange-deep));
    color: #fff;
    border: none;
    border-radius: 12px;
    font-size: 0.9rem;
    font-weight: 700;
    cursor: pointer;
    transition: transform .15s ease, box-shadow .15s ease;
    margin-top: 0.45rem;
    box-shadow: 0 10px 20px rgba(252, 128, 25, .28);
  }

  .btn:hover { transform: translateY(-1px); }
  .btn:disabled { background: #aaa; cursor: not-allowed; }

  .status { font-size: 0.85rem; margin-top: .8rem; min-height: 1.2rem; color: #555; }
  .status.error { color: #c0392b; font-weight: 600; }
  .status.ok { color: #1f8c4d; font-weight: 600; }

  .download-btn {
    display: none;
    width: 100%;
    padding: .62rem;
    background: var(--sw-green);
    color: #fff;
    border: none;
    border-radius: 12px;
    font-size: 0.87rem;
    font-weight: 700;
    cursor: pointer;
    text-align: center;
    margin-top: .72rem;
    text-decoration: none;
  }

  .gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: .7rem; }
  .gallery img {
    width: 100%;
    border-radius: 12px;
    border: 1px solid #f1e0d2;
    object-fit: contain;
    background: #fff;
    cursor: pointer;
    transition: box-shadow .15s, transform .15s;
  }

  .gallery img:hover { box-shadow: 0 4px 14px rgba(0,0,0,.15); }
  .empty {
    color: #938c85;
    font-size: 0.9rem;
    text-align: center;
    padding: 2.5rem 0;
    border: 1px dashed #ebd7c8;
    border-radius: 14px;
    background: #fff;
  }

  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid #fff; border-top-color: transparent; border-radius: 50%; animation: spin .6s linear infinite; vertical-align: middle; margin-right: 6px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes rise {
    from { opacity: 0; transform: translateY(10px); }
    to { opacity: 1; transform: translateY(0); }
  }

  #lightbox { display:none; position:fixed; inset:0; background:rgba(0,0,0,.8); z-index:100; align-items:center; justify-content:center; }
  #lightbox.open { display:flex; }
  #lightbox img { max-width:90vw; max-height:90vh; border-radius:8px; }
  #lightbox-close { position:fixed; top:1rem; right:1.2rem; color:#fff; font-size:2rem; cursor:pointer; line-height:1; }
  .file-list { list-style: none; margin-top: .5rem; }
  .file-list li { font-size: 0.82rem; color: #2b2d42; padding: .15rem 0; display:flex; align-items:center; gap:.4rem; }
  .file-list li .remove { color:#c0392b; cursor:pointer; font-weight:700; }

  @media (max-width: 920px) {
    .hero-grid { grid-template-columns: 1fr; }
    .pill-row { justify-content: flex-start; }
    main { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<section class="hero">
  <div class="container">
    <div class="topbar">
      <div class="brand">
        <span class="brand-badge">S</span>
        <span>Swiggy-Style Converter</span>
      </div>
      <div class="top-note">Fast, clean, and one-click file delivery</div>
    </div>
    <div class="hero-card">
      <div class="hero-grid">
        <div>
          <h1>Hungry for quick conversions?</h1>
          <p>Upload PDFs or images, pick your format, and get instant downloadable files with a UI inspired by modern delivery apps.</p>
        </div>
        <div class="pill-row">
          <span class="pill">Lightning Fast</span>
          <span class="pill">Secure Sessions</span>
          <span class="pill">HD Output</span>
          <span class="pill">No Signup</span>
        </div>
      </div>
    </div>
  </div>
</section>

<div class="tabs">
  <button class="tab-btn active" onclick="switchTab('pdf2img')">PDF &rarr; Image</button>
  <button class="tab-btn" onclick="switchTab('img2pdf')">Image &rarr; PDF</button>
</div>

<main id="tab-pdf2img" class="tab-content active">
  <div class="panel">
    <h2>Settings</h2>

    <div class="field">
      <div class="drop-zone" id="dropZone">
        <input type="file" id="pdfInput" accept=".pdf">
        <svg width="36" height="36" fill="none" stroke="#bbb" stroke-width="1.5" viewBox="0 0 24 24"><path d="M12 16V4m0 0L8 8m4-4 4 4"/><rect x="3" y="16" width="18" height="5" rx="1.5"/></svg>
        <p>Click or drag a PDF here</p>
        <div class="filename" id="fileName"></div>
      </div>
    </div>

    <div class="field">
      <label>Output Format</label>
      <div class="radio-group">
        <input type="radio" name="fmt" id="fmtPNG" value="PNG" checked>
        <label for="fmtPNG">PNG</label>
        <input type="radio" name="fmt" id="fmtJPEG" value="JPEG">
        <label for="fmtJPEG">JPEG</label>
      </div>
    </div>

    <div class="field">
      <label for="dpi">Resolution</label>
      <select id="dpi">
        <option value="72">72 DPI - Screen</option>
        <option value="150" selected>150 DPI - Standard</option>
        <option value="300">300 DPI - Print quality</option>
      </select>
    </div>

    <div class="field">
      <label for="pages">Page Range</label>
      <input type="text" id="pages" value="all" placeholder='all, 1-3, 1,4,6, 2-5,8'>
    </div>

    <button class="btn" id="convertBtn" onclick="convertPdf2Img()">Convert</button>
    <div id="status" class="status"></div>
    <a id="downloadBtn" class="download-btn" download>Download all as ZIP</a>
  </div>

  <div class="panel right">
    <h2>Preview</h2>
    <div id="gallery" class="empty">Converted images will appear here.</div>
  </div>
</main>

<main id="tab-img2pdf" class="tab-content">
  <div class="panel">
    <h2>Settings</h2>

    <div class="field">
      <div class="drop-zone" id="imgDropZone">
        <input type="file" id="imgInput" accept="image/*" multiple>
        <svg width="36" height="36" fill="none" stroke="#bbb" stroke-width="1.5" viewBox="0 0 24 24"><path d="M12 16V4m0 0L8 8m4-4 4 4"/><rect x="3" y="16" width="18" height="5" rx="1.5"/></svg>
        <p>Click or drag images here (multiple allowed)</p>
        <div class="filename" id="imgFileNames"></div>
      </div>
      <ul class="file-list" id="imgFileList"></ul>
    </div>

    <div class="field">
      <label for="pageSize">Page Size</label>
      <select id="pageSize">
        <option value="A4" selected>A4</option>
        <option value="Letter">Letter</option>
        <option value="A3">A3</option>
      </select>
    </div>

    <div class="field">
      <label>Orientation</label>
      <div class="radio-group">
        <input type="radio" name="orient" id="orientPortrait" value="portrait" checked>
        <label for="orientPortrait">Portrait</label>
        <input type="radio" name="orient" id="orientLandscape" value="landscape">
        <label for="orientLandscape">Landscape</label>
      </div>
    </div>

    <button class="btn" id="img2pdfBtn" onclick="convertImg2Pdf()">Convert to PDF</button>
    <div id="img2pdfStatus" class="status"></div>
    <a id="img2pdfDownload" class="download-btn" download>Download PDF</a>
  </div>

  <div class="panel right">
    <h2>Preview</h2>
    <div id="imgPreview" class="empty">Selected images will appear here.</div>
  </div>
</main>

<div id="lightbox" onclick="closeLightbox()">
  <span id="lightbox-close">&times;</span>
  <img id="lightboxImg" src="" alt="">
</div>

<script>
function switchTab(tab) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  event.target.classList.add('active');
}

const dropZone = document.getElementById("dropZone");
const pdfInput = document.getElementById("pdfInput");
const fileName = document.getElementById("fileName");

dropZone.addEventListener("click", () => pdfInput.click());
pdfInput.addEventListener("change", () => {
  if (pdfInput.files[0]) fileName.textContent = pdfInput.files[0].name;
});
dropZone.addEventListener("dragover", e => { e.preventDefault(); dropZone.classList.add("dragover"); });
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
dropZone.addEventListener("drop", e => {
  e.preventDefault(); dropZone.classList.remove("dragover");
  const f = e.dataTransfer.files[0];
  if (f && f.type === "application/pdf") {
    const dt = new DataTransfer(); dt.items.add(f);
    pdfInput.files = dt.files;
    fileName.textContent = f.name;
  }
});

async function convertPdf2Img() {
  const file = pdfInput.files[0];
  if (!file) { setStatus("status", "Please select a PDF file.", "error"); return; }

  const fmt = document.querySelector('input[name=fmt]:checked').value;
  const dpi = document.getElementById("dpi").value;
  const pages = document.getElementById("pages").value;
  const btn = document.getElementById("convertBtn");

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Converting...';
  setStatus("status", "Uploading and converting...", "");

  const form = new FormData();
  form.append("pdf", file);
  form.append("format", fmt);
  form.append("dpi", dpi);
  form.append("pages", pages);

  try {
    const res = await fetch("/convert", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) { setStatus("status", data.error || "Conversion failed.", "error"); return; }

    setStatus("status", `Converted ${data.count} page(s) to ${fmt} at ${dpi} DPI.`, "ok");
    renderGallery(data.images);
    const dl = document.getElementById("downloadBtn");
    dl.dataset.url = data.zip;
    dl.dataset.name = data.zip.split('/').pop() || "converted_images.zip";
    dl.style.display = "block";
  } catch (e) {
    setStatus("status", "Network error: " + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Convert";
  }
}

function renderGallery(images) {
  const g = document.getElementById("gallery");
  if (!images.length) { g.innerHTML = '<p class="empty">No images.</p>'; return; }
  g.className = "gallery";
  g.innerHTML = images.map(src =>
    `<img src="${src}" loading="lazy" alt="" onclick="openLightbox('${src}')">`
  ).join("");
}

const imgDropZone = document.getElementById("imgDropZone");
const imgInput = document.getElementById("imgInput");
let imgFiles = [];

imgDropZone.addEventListener("click", () => imgInput.click());
imgInput.addEventListener("change", () => addImages(imgInput.files));
imgDropZone.addEventListener("dragover", e => { e.preventDefault(); imgDropZone.classList.add("dragover"); });
imgDropZone.addEventListener("dragleave", () => imgDropZone.classList.remove("dragover"));
imgDropZone.addEventListener("drop", e => {
  e.preventDefault(); imgDropZone.classList.remove("dragover");
  addImages(e.dataTransfer.files);
});

function addImages(fileList) {
  for (const f of fileList) {
    if (f.type.startsWith("image/")) imgFiles.push(f);
  }
  renderImgList();
  renderImgPreview();
}

function removeImage(i) {
  imgFiles.splice(i, 1);
  renderImgList();
  renderImgPreview();
}

function renderImgList() {
  const ul = document.getElementById("imgFileList");
  ul.innerHTML = imgFiles.map((f, i) =>
    `<li><span class="remove" onclick="removeImage(${i})">&times;</span> ${f.name}</li>`
  ).join("");
  document.getElementById("imgFileNames").textContent =
    imgFiles.length ? imgFiles.length + " image(s) selected" : "";
}

function renderImgPreview() {
  const g = document.getElementById("imgPreview");
  if (!imgFiles.length) { g.className = "empty"; g.innerHTML = "Selected images will appear here."; return; }
  g.className = "gallery";
  g.innerHTML = "";
  imgFiles.forEach(f => {
    const img = document.createElement("img");
    img.src = URL.createObjectURL(f);
    img.loading = "lazy";
    img.onclick = () => openLightbox(img.src);
    g.appendChild(img);
  });
}

async function convertImg2Pdf() {
  if (!imgFiles.length) { setStatus("img2pdfStatus", "Please add at least one image.", "error"); return; }

  const pageSize = document.getElementById("pageSize").value;
  const orientation = document.querySelector('input[name=orient]:checked').value;
  const btn = document.getElementById("img2pdfBtn");

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Converting...';
  setStatus("img2pdfStatus", "Uploading and converting...", "");

  const form = new FormData();
  imgFiles.forEach(f => form.append("images", f));
  form.append("page_size", pageSize);
  form.append("orientation", orientation);

  try {
    const res = await fetch("/img2pdf", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) { setStatus("img2pdfStatus", data.error || "Conversion failed.", "error"); return; }

    setStatus("img2pdfStatus", `Created PDF with ${data.pages} page(s).`, "ok");
    const dl = document.getElementById("img2pdfDownload");
    dl.dataset.url = data.pdf;
    dl.dataset.name = data.pdf.split('/').pop() || "converted.pdf";
    dl.style.display = "block";
  } catch (e) {
    setStatus("img2pdfStatus", "Network error: " + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Convert to PDF";
  }
}

function setStatus(id, msg, cls) {
  const el = document.getElementById(id);
  el.textContent = msg; el.className = "status " + cls;
}

function parseDownloadName(contentDisposition, fallbackName) {
  if (!contentDisposition) return fallbackName;
  const utf8Match = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match && utf8Match[1]) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch (_) {
      return fallbackName;
    }
  }
  const plainMatch = contentDisposition.match(/filename="?([^";]+)"?/i);
  return plainMatch && plainMatch[1] ? plainMatch[1] : fallbackName;
}

async function downloadFromEndpoint(buttonId, statusId) {
  const btn = document.getElementById(buttonId);
  const url = btn.dataset.url;
  const fallbackName = btn.dataset.name || "download.bin";
  if (!url) {
    setStatus(statusId, "Download link is missing.", "error");
    return;
  }

  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) {
      throw new Error(`Download failed (${res.status})`);
    }
    const contentType = (res.headers.get("content-type") || "").toLowerCase();
    if (contentType.includes("text/html")) {
      throw new Error("Server returned an HTML page instead of a file.");
    }

    const blob = await res.blob();
    const filename = parseDownloadName(res.headers.get("content-disposition"), fallbackName);
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(blobUrl);
  } catch (e) {
    setStatus(statusId, "Download error: " + e.message, "error");
  }
}

document.getElementById("downloadBtn").addEventListener("click", (e) => {
  e.preventDefault();
  downloadFromEndpoint("downloadBtn", "status");
});

document.getElementById("img2pdfDownload").addEventListener("click", (e) => {
  e.preventDefault();
  downloadFromEndpoint("img2pdfDownload", "img2pdfStatus");
});

function openLightbox(src) {
  document.getElementById("lightboxImg").src = src;
  document.getElementById("lightbox").classList.add("open");
}

function closeLightbox() {
  document.getElementById("lightbox").classList.remove("open");
}

document.addEventListener("keydown", e => { if (e.key === "Escape") closeLightbox(); });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("Starting PDF to Image Converter...")
    print("Open http://127.0.0.1:5000 in your browser")
    app.run(host="127.0.0.1", port=5000, debug=False)
