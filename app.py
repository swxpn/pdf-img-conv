import os
import re
import json
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

try:
  from dotenv import load_dotenv
except Exception:
  load_dotenv = None

try:
  import razorpay
except Exception:
  razorpay = None

app = Flask(__name__)

if load_dotenv:
  load_dotenv()


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

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
RAZORPAY_DONATION_AMOUNT_PAISA = int(
  os.environ.get("RAZORPAY_DONATION_AMOUNT_PAISA", "9900")
)
RAZORPAY_DONATION_CURRENCY = os.environ.get("RAZORPAY_DONATION_CURRENCY", "INR").upper()
RAZORPAY_DONATION_NAME = os.environ.get("RAZORPAY_DONATION_NAME", "Swift Convert")
RAZORPAY_DONATION_DESCRIPTION = os.environ.get(
  "RAZORPAY_DONATION_DESCRIPTION", "Support Swift Convert"
)

razorpay_client = None
if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET and razorpay:
  razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
elif RAZORPAY_KEY_ID or RAZORPAY_KEY_SECRET:
  print("Razorpay keys are partially configured. Donation checkout will be disabled.")
elif not razorpay:
  print("razorpay package not available. Donation checkout will be disabled.")

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

        zip_name = "converted_images.zip"
        zip_path = os.path.join(tmp_dir, zip_name)
        image_names = []

        doc = fitz.open(pdf_path)
        try:
          total = len(doc)

          pages = parse_page_range(page_range_str, total)
          if not pages:
            return jsonify({"error": "No pages selected."}), 400

          scale = dpi / 72.0
          mat = fitz.Matrix(scale, scale)
          ext = "jpg" if fmt == "JPEG" else "png"

          with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as zf:
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
              zf.write(img_path, arcname=name)
        finally:
          doc.close()

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

@app.route("/compress-pdf", methods=["POST"])
def compress_pdf():
    pdf_file = request.files.get("pdf")
    target_percent_raw = request.form.get("target_percent", "30")
    force_compression = (
        request.form.get("force_compression", "false").strip().lower()
        in ("1", "true", "yes", "on")
    )

    if not pdf_file or pdf_file.filename == "":
        return jsonify({"error": "No PDF file provided."}), 400

    try:
        target_percent = float(target_percent_raw)
    except ValueError:
        return jsonify({"error": "Target percent must be a number."}), 400

    if target_percent < 1 or target_percent > 90:
        return jsonify({"error": "Target percent must be between 1 and 90."}), 400

    session_id = uuid.uuid4().hex
    tmp_dir = tempfile.mkdtemp(prefix=f"pdfcompress_{session_id}_")

    try:
        import shutil

        input_path = os.path.join(tmp_dir, "input.pdf")
        pdf_file.save(input_path)
        original_size = os.path.getsize(input_path)

        probe_doc = fitz.open(input_path)
        if probe_doc.needs_pass:
            probe_doc.close()
            return jsonify({"error": "Password-protected PDFs are not supported."}), 400
        probe_doc.close()

        profiles = [
            {
                "name": "light",
                "subset_fonts": False,
                "save_options": {"garbage": 1, "deflate": True, "clean": False, "linear": False},
            },
            {
                "name": "balanced",
                "subset_fonts": False,
                "save_options": {"garbage": 3, "deflate": True, "clean": True, "linear": False},
            },
            {
                "name": "aggressive",
                "subset_fonts": True,
                "save_options": {"garbage": 4, "deflate": True, "clean": True, "linear": False},
            },
        ]

        best_attempt = None
        selected_attempt = None

        for idx, profile in enumerate(profiles):
            candidate_path = os.path.join(tmp_dir, f"candidate_{idx}.pdf")
            doc = fitz.open(input_path)
            if profile["subset_fonts"] and hasattr(doc, "subset_fonts"):
                doc.subset_fonts()
            doc.save(candidate_path, **profile["save_options"])
            doc.close()

            compressed_size = os.path.getsize(candidate_path)
            reduction = 0.0
            if original_size > 0:
                reduction = (1 - (compressed_size / original_size)) * 100

            attempt = {
                "path": candidate_path,
                "size": compressed_size,
                "reduction": reduction,
                "profile": profile["name"],
            }

            if best_attempt is None or attempt["reduction"] > best_attempt["reduction"]:
                best_attempt = attempt

            if attempt["reduction"] >= target_percent and selected_attempt is None:
                selected_attempt = attempt

        if selected_attempt is None:
            selected_attempt = best_attempt

        forced_used = False
        if force_compression and selected_attempt["reduction"] < target_percent:
            force_plans = [
                {"name": "force-96dpi-q55", "dpi": 96, "jpg_quality": 55},
                {"name": "force-84dpi-q45", "dpi": 84, "jpg_quality": 45},
                {"name": "force-72dpi-q38", "dpi": 72, "jpg_quality": 38},
                {"name": "force-60dpi-q30", "dpi": 60, "jpg_quality": 30},
            ]

            force_best = None
            force_selected = None

            for idx, plan in enumerate(force_plans):
                candidate_path = os.path.join(tmp_dir, f"force_candidate_{idx}.pdf")
                src = fitz.open(input_path)
                dst = fitz.open()
                try:
                    for page in src:
                        rect = page.rect
                        out_page = dst.new_page(width=rect.width, height=rect.height)
                        scale = plan["dpi"] / 72.0
                        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                        img_bytes = pix.tobytes("jpg", jpg_quality=plan["jpg_quality"])
                        out_page.insert_image(out_page.rect, stream=img_bytes, keep_proportion=False)
                    dst.save(candidate_path, garbage=4, deflate=True, clean=True)
                finally:
                    src.close()
                    dst.close()

                compressed_size = os.path.getsize(candidate_path)
                reduction = 0.0
                if original_size > 0:
                    reduction = (1 - (compressed_size / original_size)) * 100

                attempt = {
                    "path": candidate_path,
                    "size": compressed_size,
                    "reduction": reduction,
                    "profile": plan["name"],
                }

                if force_best is None or attempt["reduction"] > force_best["reduction"]:
                    force_best = attempt

                if attempt["reduction"] >= target_percent and force_selected is None:
                    force_selected = attempt

            chosen_force_attempt = force_selected or force_best
            if chosen_force_attempt and chosen_force_attempt["reduction"] > selected_attempt["reduction"]:
                selected_attempt = chosen_force_attempt
                forced_used = True

        base = os.path.splitext(os.path.basename(pdf_file.filename))[0] or "document"
        out_name = f"compressed-{base}.pdf"
        out_path = os.path.join(tmp_dir, out_name)
        shutil.copyfile(selected_attempt["path"], out_path)

        SESSIONS[session_id] = {
            "dir": tmp_dir,
            "pdf": out_name,
            "created": time.time(),
        }

        return jsonify(
            {
                "session": session_id,
                "pdf": f"/download/{session_id}/{out_name}",
                "original_bytes": original_size,
                "compressed_bytes": selected_attempt["size"],
                "reduction_percent": round(selected_attempt["reduction"], 2),
                "target_percent": round(target_percent, 2),
                "achieved_target": selected_attempt["reduction"] >= target_percent,
                "profile": selected_attempt["profile"],
                "force_requested": force_compression,
                "forced_used": forced_used,
            }
        )

    except Exception as e:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": f"Compression failed: {e}"}), 500

@app.route("/compress-image", methods=["POST"])
def compress_image():
    image_file = request.files.get("image")
    target_percent_raw = request.form.get("target_percent", "30")
    force_compression = (
        request.form.get("force_compression", "false").strip().lower()
        in ("1", "true", "yes", "on")
    )

    if not image_file or image_file.filename == "":
        return jsonify({"error": "No image file provided."}), 400

    ext = os.path.splitext(image_file.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return jsonify({"error": "Unsupported image format."}), 400

    try:
        target_percent = float(target_percent_raw)
    except ValueError:
        return jsonify({"error": "Target percent must be a number."}), 400

    if target_percent < 1 or target_percent > 90:
        return jsonify({"error": "Target percent must be between 1 and 90."}), 400

    session_id = uuid.uuid4().hex
    tmp_dir = tempfile.mkdtemp(prefix=f"imgcompress_{session_id}_")

    try:
        import shutil

        input_path = os.path.join(tmp_dir, f"input{ext if ext else '.img'}")
        image_file.save(input_path)
        original_size = os.path.getsize(input_path)

        with Image.open(input_path) as source_img:
            source_img.load()
            has_alpha = "A" in source_img.getbands()

            profiles = [
                {"name": "light", "quality": 88, "scale": 1.0, "png_colors": None},
                {"name": "balanced", "quality": 76, "scale": 1.0, "png_colors": 192},
                {"name": "aggressive", "quality": 64, "scale": 0.92, "png_colors": 128},
            ]

            best_attempt = None
            selected_attempt = None

            for idx, profile in enumerate(profiles):
                candidate_path = os.path.join(tmp_dir, f"img_candidate_{idx}")
                img = source_img.copy()

                if profile["scale"] < 1.0:
                    w = max(1, int(img.width * profile["scale"]))
                    h = max(1, int(img.height * profile["scale"]))
                    img = img.resize((w, h), Image.LANCZOS)

                if has_alpha:
                    if img.mode not in ("RGBA", "LA"):
                        img = img.convert("RGBA")
                    if profile["png_colors"]:
                        img = img.convert("P", palette=Image.ADAPTIVE, colors=profile["png_colors"])
                    out_ext = "png"
                    out_file = f"{candidate_path}.png"
                    img.save(out_file, format="PNG", optimize=True, compress_level=9)
                else:
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    out_ext = "jpg"
                    out_file = f"{candidate_path}.jpg"
                    img.save(
                        out_file,
                        format="JPEG",
                        quality=profile["quality"],
                        optimize=True,
                        progressive=True,
                    )

                compressed_size = os.path.getsize(out_file)
                reduction = 0.0
                if original_size > 0:
                    reduction = (1 - (compressed_size / original_size)) * 100

                attempt = {
                    "path": out_file,
                    "size": compressed_size,
                    "reduction": reduction,
                    "profile": profile["name"],
                    "ext": out_ext,
                }

                if best_attempt is None or attempt["reduction"] > best_attempt["reduction"]:
                    best_attempt = attempt

                if attempt["reduction"] >= target_percent and selected_attempt is None:
                    selected_attempt = attempt

            if selected_attempt is None:
                selected_attempt = best_attempt

            forced_used = False
            if force_compression and selected_attempt["reduction"] < target_percent:
                force_plans = [
                    {"name": "force-75", "quality": 75, "scale": 0.9, "png_colors": 96},
                    {"name": "force-60", "quality": 60, "scale": 0.82, "png_colors": 64},
                    {"name": "force-45", "quality": 45, "scale": 0.72, "png_colors": 48},
                    {"name": "force-35", "quality": 35, "scale": 0.62, "png_colors": 32},
                ]

                force_best = None
                force_selected = None

                for idx, plan in enumerate(force_plans):
                    candidate_path = os.path.join(tmp_dir, f"img_force_candidate_{idx}")
                    img = source_img.copy()
                    w = max(1, int(img.width * plan["scale"]))
                    h = max(1, int(img.height * plan["scale"]))
                    img = img.resize((w, h), Image.LANCZOS)

                    if has_alpha:
                        if img.mode not in ("RGBA", "LA"):
                            img = img.convert("RGBA")
                        img = img.convert("P", palette=Image.ADAPTIVE, colors=plan["png_colors"])
                        out_ext = "png"
                        out_file = f"{candidate_path}.png"
                        img.save(out_file, format="PNG", optimize=True, compress_level=9)
                    else:
                        if img.mode != "RGB":
                            img = img.convert("RGB")
                        out_ext = "jpg"
                        out_file = f"{candidate_path}.jpg"
                        img.save(
                            out_file,
                            format="JPEG",
                            quality=plan["quality"],
                            optimize=True,
                            progressive=True,
                        )

                    compressed_size = os.path.getsize(out_file)
                    reduction = 0.0
                    if original_size > 0:
                        reduction = (1 - (compressed_size / original_size)) * 100

                    attempt = {
                        "path": out_file,
                        "size": compressed_size,
                        "reduction": reduction,
                        "profile": plan["name"],
                        "ext": out_ext,
                    }

                    if force_best is None or attempt["reduction"] > force_best["reduction"]:
                        force_best = attempt

                    if attempt["reduction"] >= target_percent and force_selected is None:
                        force_selected = attempt

                chosen_force_attempt = force_selected or force_best
                if chosen_force_attempt and chosen_force_attempt["reduction"] > selected_attempt["reduction"]:
                    selected_attempt = chosen_force_attempt
                    forced_used = True

        base = os.path.splitext(os.path.basename(image_file.filename))[0] or "image"
        out_name = f"compressed-{base}.{selected_attempt['ext']}"
        out_path = os.path.join(tmp_dir, out_name)
        shutil.copyfile(selected_attempt["path"], out_path)

        SESSIONS[session_id] = {
            "dir": tmp_dir,
            "image": out_name,
            "created": time.time(),
        }

        return jsonify(
            {
                "session": session_id,
                "image": f"/download/{session_id}/{out_name}",
                "original_bytes": original_size,
                "compressed_bytes": selected_attempt["size"],
                "reduction_percent": round(selected_attempt["reduction"], 2),
                "target_percent": round(target_percent, 2),
                "achieved_target": selected_attempt["reduction"] >= target_percent,
                "force_requested": force_compression,
                "forced_used": forced_used,
            }
        )

    except Exception as e:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": f"Image compression failed: {e}"}), 500


@app.route("/modify-pdf", methods=["POST"])
def modify_pdf():
    operation = request.form.get("operation", "").strip().lower()
    allowed_operations = {"merge", "visual_edit"}
    if operation not in allowed_operations:
        return jsonify({"error": "Invalid PDF edit operation."}), 400

    session_id = uuid.uuid4().hex
    tmp_dir = tempfile.mkdtemp(prefix=f"pdfedit_{session_id}_")

    try:
        import shutil

        uploaded_pdfs = [
            file
            for file in request.files.getlist("pdfs")
            if file and file.filename and file.filename.lower().endswith(".pdf")
        ]
        single_pdf = request.files.get("pdf")
        if (
            single_pdf
            and single_pdf.filename
            and single_pdf.filename.lower().endswith(".pdf")
        ):
            uploaded_pdfs = [single_pdf]

        output_name = ""
        summary = ""
        output_count = 1
        page_count = 0

        if operation == "merge":
            if len(uploaded_pdfs) < 2:
                return jsonify({"error": "Merge requires at least two PDF files."}), 400

            merged_doc = fitz.open()
            merged_pages = 0
            try:
                for index, pdf_file in enumerate(uploaded_pdfs):
                    input_path = os.path.join(tmp_dir, f"merge_{index}.pdf")
                    pdf_file.save(input_path)
                    source_doc = fitz.open(input_path)
                    try:
                        if source_doc.needs_pass:
                            return jsonify(
                                {"error": "Password-protected PDFs are not supported."}
                            ), 400
                        merged_doc.insert_pdf(source_doc)
                        merged_pages += len(source_doc)
                    finally:
                        source_doc.close()

                output_name = "merged-document.pdf"
                output_path = os.path.join(tmp_dir, output_name)
                merged_doc.save(output_path)
            finally:
                merged_doc.close()

            summary = f"Merged {len(uploaded_pdfs)} PDFs into one file."
            page_count = merged_pages

        else:
            if len(uploaded_pdfs) != 1:
                return jsonify(
                    {"error": "This operation requires exactly one PDF file."}
                ), 400

            pdf_file = uploaded_pdfs[0]
            input_path = os.path.join(tmp_dir, "input.pdf")
            pdf_file.save(input_path)
            source_doc = fitz.open(input_path)
            if source_doc.needs_pass:
                source_doc.close()
                return jsonify({"error": "Password-protected PDFs are not supported."}), 400

            total_pages = len(source_doc)
            base_name = os.path.splitext(os.path.basename(pdf_file.filename))[0] or "document"

            slots_raw = (request.form.get("slots_json", "") or "").strip()
            if not slots_raw:
              raise ValueError("No page edits were provided.")

            try:
                slots = json.loads(slots_raw)
            except json.JSONDecodeError as e:
              raise ValueError(f"Invalid edit payload: {e}")

            if not isinstance(slots, list) or not slots:
              raise ValueError("Edit must include at least one page slot.")

            edited_doc = fitz.open()
            try:
                for slot in slots:
                    if not isinstance(slot, dict):
                      raise ValueError("Each slot must be an object.")

                    page_number = int(slot.get("page", 0))
                    rotation = int(slot.get("rotation", 0))
                    if page_number < 1 or page_number > total_pages:
                        raise ValueError(
                            f"Page {page_number} out of bounds (doc has {total_pages} pages)"
                        )
                    if rotation not in (0, 90, 180, 270):
                      raise ValueError(
                        "Rotation must be one of 0, 90, 180, or 270."
                      )

                    edited_doc.insert_pdf(
                        source_doc,
                        from_page=page_number - 1,
                        to_page=page_number - 1,
                    )
                    edited_doc[-1].set_rotation(rotation)

                output_name = f"edited-{base_name}.pdf"
                output_path = os.path.join(tmp_dir, output_name)
                edited_doc.save(output_path)
            finally:
                edited_doc.close()

            page_count = len(slots)
            summary = f"Applied page edits to {len(slots)} slot(s)."
 
            source_doc.close()

        SESSIONS[session_id] = {
            "dir": tmp_dir,
            "file": output_name,
            "created": time.time(),
        }

        return jsonify(
            {
                "session": session_id,
                "operation": operation,
                "file": f"/download/{session_id}/{output_name}",
                "filename": output_name,
                "page_count": page_count,
                "output_count": output_count,
                "summary": summary,
            }
        )

    except ValueError as e:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": f"PDF edit failed: {e}"}), 500


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


@app.route("/donate/order", methods=["POST"])
def create_donation_order():
    if not razorpay_client or not RAZORPAY_KEY_ID:
        return jsonify({"error": "Razorpay is not configured on this server."}), 503

    payload = request.get_json(silent=True) or {}
    raw_amount = payload.get("amount")
    amount = RAZORPAY_DONATION_AMOUNT_PAISA

    if raw_amount is not None:
        try:
            amount = int(raw_amount)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid amount."}), 400

    if amount < 100:
        return jsonify({"error": "Minimum donation amount is 100 paise."}), 400

    try:
        order = razorpay_client.order.create(
            {
                "amount": amount,
                "currency": RAZORPAY_DONATION_CURRENCY,
                "receipt": f"don_{uuid.uuid4().hex[:18]}",
                "payment_capture": 1,
            }
        )
    except Exception as exc:
        print(f"Failed to create Razorpay order: {exc}")
        return jsonify({"error": "Unable to create payment order right now."}), 500

    return jsonify(
        {
            "key": RAZORPAY_KEY_ID,
            "order_id": order.get("id"),
            "amount": order.get("amount"),
            "currency": order.get("currency"),
            "name": RAZORPAY_DONATION_NAME,
            "description": RAZORPAY_DONATION_DESCRIPTION,
        }
    )


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PDF &amp; Image Converter</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="shortcut icon" href="/static/favicon.svg">
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
    --sw-surface-soft: #fff6ef;
    --sw-surface-tint: #fff4eb;
    --sw-surface-alt: #fff9f4;
    --sw-accent-wash: #ffd8bb;
    --sw-shadow: rgba(31, 31, 36, .06);
    --sw-hero-shadow: rgba(252, 128, 25, .13);
    --sw-tab-shadow: rgba(252, 128, 25, .36);
    --sw-btn-shadow: rgba(252, 128, 25, .28);
    --sw-gallery-shadow: rgba(0, 0, 0, .15);
    --sw-overlay: rgba(0, 0, 0, .8);
  }

  body.dark-mode {
    --sw-ink: #f4eee9;
    --sw-muted: #bdaea2;
    --sw-bg: #131110;
    --sw-card: #1d1917;
    --sw-line: #3c3028;
    --sw-green: #31b96d;
    --sw-surface-soft: #241d1a;
    --sw-surface-tint: #2b221d;
    --sw-surface-alt: #201a17;
    --sw-accent-wash: #5e4128;
    --sw-shadow: rgba(0, 0, 0, .32);
    --sw-hero-shadow: rgba(0, 0, 0, .28);
    --sw-tab-shadow: rgba(252, 128, 25, .22);
    --sw-btn-shadow: rgba(252, 128, 25, .22);
    --sw-gallery-shadow: rgba(0, 0, 0, .3);
    --sw-overlay: rgba(0, 0, 0, .88);
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: "Poppins", sans-serif;
    color: var(--sw-ink);
    min-height: 100vh;
    padding-bottom: 72px;
    transition: background .22s ease, color .22s ease;
    background:
      radial-gradient(circle at 85% -10%, rgba(255, 216, 189, .95) 0%, rgba(255, 216, 189, 0) 40%),
      radial-gradient(circle at 10% 0%, rgba(255, 232, 215, .95) 0%, rgba(255, 232, 215, 0) 42%),
      var(--sw-bg);
  }

  body.dark-mode {
    background:
      radial-gradient(circle at 85% -10%, rgba(252, 128, 25, .22) 0%, rgba(252, 128, 25, 0) 38%),
      radial-gradient(circle at 10% 0%, rgba(245, 144, 71, .14) 0%, rgba(245, 144, 71, 0) 42%),
      linear-gradient(180deg, #171310 0%, var(--sw-bg) 100%);
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

  .brand-title-wrap {
    display: flex;
    flex-direction: column;
    gap: 0;
    line-height: 1;
  }

  .brand-powered {
    font-size: .65rem;
    font-weight: 500;
    letter-spacing: .4px;
    color: var(--sw-muted);
    text-transform: lowercase;
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
    border: none;
    cursor: pointer;
    transition: transform .15s ease, box-shadow .15s ease;
  }

  .brand-badge:hover {
    transform: translateY(-1px);
    box-shadow: 0 12px 24px rgba(252, 128, 25, .38);
  }

  .top-note {
    color: var(--sw-muted);
    font-size: .83rem;
    font-weight: 500;
  }

  .donate-bar {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    display: flex;
    justify-content: center;
    padding: .6rem 1rem;
    background: var(--sw-surface);
    border-top: 1px solid var(--sw-line);
    z-index: 999;
  }

  .donate-modal {
    position: fixed;
    inset: 0;
    display: none;
    align-items: center;
    justify-content: center;
    padding: 1rem;
    background: rgba(0, 0, 0, .45);
    z-index: 1200;
  }

  .donate-modal.open {
    display: flex;
  }

  .donate-modal-card {
    width: min(360px, 100%);
    background: var(--sw-card);
    border: 1px solid var(--sw-line);
    border-radius: 16px;
    padding: 1rem;
    box-shadow: 0 18px 40px var(--sw-shadow);
  }

  .donate-modal-card h3 {
    font-size: 1rem;
    margin-bottom: .65rem;
  }

  .donate-modal-card p {
    color: var(--sw-muted);
    font-size: .82rem;
    margin-bottom: .7rem;
  }

  .donate-amount {
    width: 100%;
    padding: .6rem .8rem;
    border-radius: 10px;
    border: 1px solid var(--sw-line);
    background: var(--sw-card);
    color: var(--sw-ink);
    font-size: .95rem;
    font-weight: 600;
    outline: none;
  }

  .donate-amount:focus {
    border-color: var(--sw-orange);
    box-shadow: 0 0 0 3px rgba(252, 128, 25, .2);
  }

  .donate-btn {
    display: inline-flex;
    align-items: center;
    gap: .45rem;
    padding: .45rem 1.1rem;
    border-radius: 999px;
    border: 1px solid var(--sw-line);
    background: var(--sw-card);
    color: var(--sw-text);
    font-size: .8rem;
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
    transition: background .15s ease, transform .15s ease, box-shadow .15s ease;
  }

  .donate-btn:hover {
    background: var(--sw-orange);
    color: #fff;
    border-color: transparent;
    transform: translateY(-1px);
    box-shadow: 0 6px 18px rgba(252,128,25,.35);
  }

  .donate-actions {
    display: flex;
    gap: .5rem;
    margin-top: .75rem;
  }

  .donate-btn.alt {
    background: transparent;
    color: var(--sw-ink);
  }

  .topbar-actions {
    display: flex;
    align-items: center;
    gap: .75rem;
  }

  .hero-card {
    background: linear-gradient(120deg, var(--sw-card) 0%, var(--sw-surface-soft) 100%);
    border: 1px solid var(--sw-line);
    border-radius: 24px;
    padding: 1.4rem 1.25rem;
    box-shadow: 0 18px 38px var(--sw-hero-shadow);
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
    gap: .5rem .7rem;
    justify-content: flex-end;
  }

  .pill {
    display: inline-flex;
    align-items: center;
    gap: .38rem;
    border: none;
    background: transparent;
    color: var(--sw-muted);
    padding: .1rem 0;
    border-radius: 0;
    font-size: .74rem;
    font-weight: 700;
    letter-spacing: .35px;
    text-transform: uppercase;
    animation: rise .5s ease-out both;
  }

  .pill::before {
    content: "";
    width: 7px;
    height: 7px;
    border-radius: 999px;
    background: linear-gradient(135deg, var(--sw-orange), var(--sw-orange-deep));
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--sw-orange) 18%, transparent);
    flex: 0 0 auto;
  }
  .pill:nth-child(1) { animation-delay: .1s; }
  .pill:nth-child(2) { animation-delay: .17s; }
  .pill:nth-child(3) { animation-delay: .24s; }
  .pill:nth-child(4) { animation-delay: .31s; }

  .tabs {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: .6rem;
    max-width: 1140px;
    margin: .7rem auto 0;
    padding: 0 1rem;
  }
  .tab-btn {
    padding: .66rem 1.1rem;
    background: var(--sw-card);
    border: 1px solid var(--sw-line);
    cursor: pointer;
    font-size: 0.86rem;
    font-weight: 700;
    color: var(--sw-muted);
    border-radius: 999px;
    transition: all .2s ease;
    animation: rise .45s ease-out both;
  }
  .tab-btn:nth-child(1) { animation-delay: .04s; }
  .tab-btn:nth-child(2) { animation-delay: .09s; }
  .tab-btn:nth-child(3) { animation-delay: .14s; }
  .tab-btn:nth-child(4) { animation-delay: .19s; }
  .tab-btn:nth-child(5) { animation-delay: .24s; }
  .tab-btn:hover:not(.active) { transform: translateY(-1px); box-shadow: 0 4px 12px var(--sw-shadow); }
  .tab-btn.active {
    background: linear-gradient(135deg, var(--sw-orange), var(--sw-orange-deep));
    color: #fff;
    border-color: transparent;
    box-shadow: 0 11px 24px var(--sw-tab-shadow);
  }

  .tab-content { display: none; }
  .tab-content.active { display: grid; animation: tab-enter .3s ease-out; }

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
    border: 1px solid var(--sw-line);
    box-shadow: 0 10px 30px var(--sw-shadow);
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
    color: var(--sw-muted);
    text-transform: uppercase;
    letter-spacing: .35px;
  }

  .field { margin-bottom: .85rem; }
  .hidden { display: none !important; }

  .field-note {
    font-size: .79rem;
    line-height: 1.45;
    color: var(--sw-muted);
  }

  .merge-tip-note {
    margin-top: .5rem;
    padding: .45rem .55rem;
    border-left: 3px solid var(--sw-orange);
    border-radius: 8px;
    background: var(--sw-surface-tint);
    color: var(--sw-ink);
    font-size: .79rem;
    line-height: 1.45;
  }

  .field-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: .65rem;
  }

  .range-wrap {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: .65rem;
    align-items: center;
  }

  input[type=range] {
    accent-color: var(--sw-orange);
    width: 100%;
  }

  .range-value {
    min-width: 64px;
    text-align: center;
    font-weight: 700;
    font-size: .84rem;
    color: var(--sw-ink);
    background: var(--sw-surface-tint);
    border: 1px solid var(--sw-accent-wash);
    border-radius: 10px;
    padding: .32rem .5rem;
  }

  .force-row {
    display: flex;
    align-items: center;
    gap: .55rem;
    margin-top: .2rem;
  }

  .force-row input[type=checkbox] {
    width: 16px;
    height: 16px;
    accent-color: var(--sw-orange);
  }

  .check-label {
    margin: 0;
    text-transform: none;
    letter-spacing: 0;
    font-size: .82rem;
    font-weight: 600;
    color: var(--sw-muted);
  }

  input[type=text], input[type=number], select {
    width: 100%;
    padding: .66rem .72rem;
    border: 1px solid var(--sw-line);
    border-radius: 11px;
    font-size: 0.9rem;
    outline: none;
    transition: border .15s, box-shadow .15s;
    background: var(--sw-card);
    color: var(--sw-ink);
  }

  input[type=text]:focus, input[type=number]:focus, select:focus {
    border-color: #ffc799;
    box-shadow: 0 0 0 4px rgba(252, 128, 25, .13);
  }

  .radio-group { display: flex; gap: .6rem; }
  .radio-group label {
    flex: 1;
    text-align: center;
    padding: .5rem;
    border: 1px solid var(--sw-line);
    border-radius: 11px;
    cursor: pointer;
    font-size: 0.83rem;
    font-weight: 600;
    transition: all .15s;
    color: var(--sw-ink);
    text-transform: none;
    letter-spacing: 0;
    margin-bottom: 0;
    background: var(--sw-card);
  }

  .radio-group input { display: none; }
  .radio-group input:checked + label {
    background: var(--sw-surface-tint);
    color: var(--sw-ink);
    border-color: #ffc799;
  }
  .radio-group label:hover { transform: translateY(-1px); }

  .drop-zone {
    border: 2px dashed #ffcda2;
    border-radius: 14px;
    padding: 1.4rem .75rem;
    text-align: center;
    cursor: pointer;
    transition: all .18s;
    margin-bottom: .75rem;
    background: linear-gradient(180deg, var(--sw-surface-alt) 0%, var(--sw-card) 100%);
  }

  .drop-zone svg { transition: transform .22s ease; }
  .drop-zone:hover { transform: translateY(-2px); box-shadow: 0 8px 22px var(--sw-shadow); }
  .drop-zone:hover svg { transform: translateY(-4px) scale(1.12); }
  .drop-zone.dragover { border-color: var(--sw-orange); background: var(--sw-surface-tint); }
  .drop-zone input { display: none; }

  .drop-zone p { font-size: 0.84rem; color: var(--sw-muted); margin-top: 0.48rem; }
  .drop-zone .filename { font-size: 0.84rem; color: var(--sw-ink); font-weight: 600; margin-top: .46rem; }

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
    box-shadow: 0 10px 20px var(--sw-btn-shadow);
  }

  .btn:hover { transform: translateY(-1px); box-shadow: 0 14px 26px var(--sw-btn-shadow); }
  .btn:active:not(:disabled) { transform: translateY(0) scale(.97); box-shadow: 0 6px 14px var(--sw-btn-shadow); }
  .btn:disabled { background: #aaa; cursor: not-allowed; }

  .status { font-size: 0.85rem; margin-top: .8rem; min-height: 1.2rem; color: var(--sw-muted); animation: rise .3s ease-out; }
  .status.error { color: #c0392b; font-weight: 600; }
  .status.ok { color: #bdaea2; font-weight: 600; }

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
    transition: transform .15s ease, box-shadow .15s ease;
    animation: rise .35s ease-out;
  }
  .download-btn:hover { transform: translateY(-1px); box-shadow: 0 8px 20px rgba(31, 177, 109, .32); }
  .download-btn:active { transform: translateY(0) scale(.97); }

  .gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: .7rem; }
  .gallery img {
    width: 100%;
    border-radius: 12px;
    border: 1px solid var(--sw-line);
    object-fit: contain;
    background: var(--sw-card);
    cursor: pointer;
    transition: box-shadow .2s ease, transform .2s ease;
    animation: rise .4s ease-out both;
  }

  .gallery img:hover { box-shadow: 0 6px 18px var(--sw-gallery-shadow); transform: translateY(-3px) scale(1.02); }
  .empty {
    color: var(--sw-muted);
    font-size: 0.9rem;
    text-align: center;
    padding: 2.5rem 0;
    border: 1px dashed var(--sw-line);
    border-radius: 14px;
    background: var(--sw-card);
  }

  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid #fff; border-top-color: transparent; border-radius: 50%; animation: spin .6s linear infinite; vertical-align: middle; margin-right: 6px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes rise {
    from { opacity: 0; transform: translateY(10px); }
    to { opacity: 1; transform: translateY(0); }
  }
  @keyframes pop-in {
    from { opacity: 0; transform: scale(.88); }
    to { opacity: 1; transform: scale(1); }
  }
  @keyframes tab-enter {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  @keyframes lightbox-bg-in {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  #lightbox { display:none; position:fixed; inset:0; background:var(--sw-overlay); z-index:100; align-items:center; justify-content:center; }
  #lightbox.open { display:flex; animation: lightbox-bg-in .22s ease-out; }
  #lightbox img { max-width:90vw; max-height:90vh; border-radius:8px; transition: transform .25s ease; }
  #lightbox.open img { animation: pop-in .28s ease-out; }
  #lightbox-close { position:fixed; top:1rem; right:1.2rem; color:#fff; font-size:2rem; cursor:pointer; line-height:1; transition: transform .15s ease, opacity .15s ease; }
  #lightbox-close:hover { transform: scale(1.15); opacity: .8; }
  .file-list { list-style: none; margin-top: .5rem; }
  .file-list li { font-size: 0.82rem; color: var(--sw-ink); padding: .15rem 0; display:flex; align-items:center; gap:.4rem; animation: rise .3s ease-out both; }
  .file-list li .remove { color:#c0392b; cursor:pointer; font-weight:700; }
  .file-list li.draggable-file {
    padding: .45rem .55rem;
    border: 1px solid var(--sw-line);
    border-radius: 10px;
    background: var(--sw-card);
    cursor: grab;
    transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease, background-color .16s ease;
  }
  .file-list li.draggable-file:hover {
    transform: translateY(-1px);
    box-shadow: 0 8px 18px rgba(36, 28, 21, .09);
  }
  .file-list li.draggable-file.dragging {
    opacity: .5;
    transform: scale(1.01);
    border-color: var(--sw-orange);
  }
  .file-list li.draggable-file.drag-target {
    border-color: var(--sw-orange);
    background: var(--sw-surface-tint);
    box-shadow: 0 0 0 2px rgba(230, 126, 34, .12) inset;
  }
  .file-order-badge {
    min-width: 1.4rem;
    height: 1.4rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 999px;
    background: var(--sw-surface-tint);
    color: var(--sw-ink);
    font-size: .7rem;
    font-weight: 700;
  }

  .btn-secondary {
    display: inline-block;
    width: auto;
    padding: .52rem .72rem;
    margin-top: .35rem;
    border-radius: 10px;
    border: 1px solid var(--sw-line);
    background: var(--sw-card);
    color: var(--sw-ink);
    font-size: .8rem;
    font-weight: 700;
    cursor: pointer;
    transition: background .15s ease, border-color .15s ease, transform .12s ease, box-shadow .12s ease;
  }
  .btn-secondary:hover { transform: translateY(-1px); box-shadow: 0 5px 14px var(--sw-shadow); border-color: var(--sw-orange); }
  .btn-secondary:active { transform: translateY(0) scale(.97); }

  .pdf-visual-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: .65rem;
    margin-top: .55rem;
  }

  .pdf-page-card {
    border: 1px solid var(--sw-line);
    border-radius: 12px;
    background: var(--sw-card);
    padding: .5rem;
    cursor: grab;
    transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease, background-color .16s ease;
  }

  .pdf-page-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 10px 22px rgba(36, 28, 21, .12);
  }

  .pdf-page-card.dragging {
    opacity: .5;
    transform: rotate(1deg) scale(1.02);
    border-color: var(--sw-orange);
    box-shadow: 0 16px 26px rgba(36, 28, 21, .18);
  }

  .pdf-page-card.drag-target {
    border-color: var(--sw-orange);
    background: var(--sw-surface-tint);
    box-shadow: 0 0 0 2px rgba(230, 126, 34, .18) inset;
  }

  .pdf-thumb {
    width: 100%;
    border-radius: 8px;
    border: 1px solid var(--sw-line);
    background: var(--sw-surface-alt);
    display: block;
    margin-bottom: .45rem;
  }

  .pdf-page-meta {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: .75rem;
    color: var(--sw-muted);
    margin-bottom: .35rem;
  }

  .pdf-page-actions {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: .3rem;
  }

  .mini-btn {
    padding: .38rem .3rem;
    border: 1px solid var(--sw-line);
    border-radius: 8px;
    background: var(--sw-surface-tint);
    color: var(--sw-ink);
    font-size: .75rem;
    font-weight: 700;
    cursor: pointer;
    transition: background .15s ease, border-color .15s ease, transform .12s ease;
  }
  .mini-btn:hover { transform: scale(1.05); background: var(--sw-accent-wash); border-color: var(--sw-orange); }
  .mini-btn:active { transform: scale(.97); }

  .tick-btn {
    width: 100%;
    padding: .36rem .3rem;
    border: 1px solid var(--sw-line);
    border-radius: 8px;
    font-size: .74rem;
    font-weight: 700;
    cursor: pointer;
    color: var(--sw-ink);
    background: var(--sw-surface-alt);
    transition: background .18s ease, color .18s ease, border-color .18s ease, transform .12s ease;
  }
  .tick-btn:hover { transform: scale(1.03); }
  .tick-btn:active { transform: scale(.96); }

  .tick-btn.on {
    color: #fff;
    border-color: transparent;
    background: linear-gradient(135deg, var(--sw-green), #1f8c4d);
  }

  .pdf-rotation {
    text-align: center;
    margin-top: .3rem;
    font-size: .73rem;
    color: var(--sw-muted);
  }

  @media (max-width: 920px) {
    .hero-grid { grid-template-columns: 1fr; }
    .pill-row { justify-content: flex-start; }
    main { grid-template-columns: 1fr; }
    .topbar { align-items: flex-start; }
    .topbar-actions { width: 100%; }
    .field-grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<section class="hero">
  <div class="container">
    <div class="topbar">
      <div class="brand">
        <button type="button" id="themeToggle" class="brand-badge" aria-label="Toggle theme" title="Toggle theme"><span id="themeIcon">☀</span></button>
        <span class="brand-title-wrap"><span>Swift Convert</span><span class="brand-powered">powered by vercel</span></span>
      </div>
      <div class="topbar-actions">
        <div class="top-note">Fast, clean, and one-click file delivery</div>
      </div>
    </div>
    <div class="hero-card">
      <div class="hero-grid">
        <div>
          <h1>Quick conversions?</h1>
          <p>Upload PDFs or images, pick your format, and get instantly.</p>
        </div>
        <div class="pill-row">
          <span class="pill">Secure Sessions</span>
          <span class="pill">No Watermark</span>
          <span class="pill">No Signup</span>
        </div>
      </div>
    </div>
  </div>
</section>

<div class="tabs">
  <button class="tab-btn active" onclick="switchTab('pdf2img')">PDF &rarr; Image</button>
  <button class="tab-btn" onclick="switchTab('img2pdf')">Image &rarr; PDF</button>
  <button class="tab-btn" onclick="switchTab('pdfedit')">Edit PDF</button>
  <button class="tab-btn" onclick="switchTab('pdfcompress')">Compress PDF</button>
  <button class="tab-btn" onclick="switchTab('imgcompress')">Compress Image</button>
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
      <input type="text" id="pages" placeholder="e.g. all, 1-3,5">
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

<main id="tab-imgcompress" class="tab-content">
  <div class="panel">
    <h2>Image Compression Settings</h2>

    <div class="field">
      <div class="drop-zone" id="compressImgDropZone">
        <input type="file" id="compressImgInput" accept="image/*">
        <svg width="36" height="36" fill="none" stroke="#bbb" stroke-width="1.5" viewBox="0 0 24 24"><path d="M12 16V4m0 0L8 8m4-4 4 4"/><rect x="3" y="16" width="18" height="5" rx="1.5"/></svg>
        <p>Click or drag an image here</p>
        <div class="filename" id="compressImgFileName"></div>
      </div>
    </div>

    <div class="field">
      <label for="imgTargetReduction">Target Reduction</label>
      <div class="range-wrap">
        <input type="range" id="imgTargetReduction" min="5" max="80" step="5" value="30">
        <span id="imgTargetReductionValue" class="range-value">30%</span>
      </div>
    </div>

    <div class="field">
      <div class="force-row">
        <input type="checkbox" id="imgForceCompression">
        <label for="imgForceCompression" class="check-label">Force compression (may reduce image quality)</label>
      </div>
    </div>

    <button class="btn" id="imgCompressBtn" onclick="compressImageFile()">Compress Image</button>
    <div id="imgCompressStatus" class="status"></div>
    <a id="imgCompressDownload" class="download-btn" download>Download Compressed Image</a>
  </div>

  <div class="panel right">
    <h2>Compression Result</h2>
    <div id="imgCompressResult" class="empty">Image compression details will appear here.</div>
  </div>
</main>

<main id="tab-pdfedit" class="tab-content">
  <div class="panel">
    <h2>Edit Pages</h2>

    <div class="field">
      <div class="drop-zone" id="pdfEditDropZone">
        <input type="file" id="pdfEditInput" accept=".pdf" multiple>
        <svg width="36" height="36" fill="none" stroke="#bbb" stroke-width="1.5" viewBox="0 0 24 24"><path d="M12 16V4m0 0L8 8m4-4 4 4"/><rect x="3" y="16" width="18" height="5" rx="1.5"/></svg>
        <p id="pdfEditDropText">Click or drag a PDF here</p>
        <div class="filename" id="pdfEditFileNames"></div>
      </div>
      <ul class="file-list" id="pdfEditFileList"></ul>
    </div>

    <div class="field">
      <div id="pdfEditOperationHelp" class="field-note">Load one PDF and drag cards to reorder pages. Use tick and rotate controls on each page card.</div>
      <div class="merge-tip-note"><strong>Tip:</strong> upload two or more PDFs here to merge them into one file.</div>
    </div>

    <button class="btn" id="pdfEditBtn" onclick="modifyPdf()">Apply Page Edit</button>
    <div id="pdfEditStatus" class="status"></div>
    <a id="pdfEditDownload" class="download-btn" download>Download Edited PDF</a>
  </div>

  <div class="panel right">
    <h2 id="pdfEditSideTitle">Page Thumbnails</h2>
    <div id="pdfMergeInfo" class="empty hidden">Merge mode combines all selected PDFs in the listed order.</div>
    <div class="field" id="pdfVisualField">
      <button type="button" class="btn-secondary" id="pdfVisualLoadBtn">Load Page Thumbnails</button>
      <div id="pdfVisualGrid" class="pdf-visual-grid"></div>
    </div>
  </div>
</main>

<main id="tab-pdfcompress" class="tab-content">
  <div class="panel">
    <h2>Compression Settings</h2>

    <div class="field">
      <div class="drop-zone" id="compressDropZone">
        <input type="file" id="compressInput" accept=".pdf">
        <svg width="36" height="36" fill="none" stroke="#bbb" stroke-width="1.5" viewBox="0 0 24 24"><path d="M12 16V4m0 0L8 8m4-4 4 4"/><rect x="3" y="16" width="18" height="5" rx="1.5"/></svg>
        <p>Click or drag a PDF here</p>
        <div class="filename" id="compressFileName"></div>
      </div>
    </div>

    <div class="field">
      <label for="targetReduction">Target Reduction</label>
      <div class="range-wrap">
        <input type="range" id="targetReduction" min="5" max="80" step="5" value="30">
        <span id="targetReductionValue" class="range-value">30%</span>
      </div>
    </div>

    <div class="field">
      <div class="force-row">
        <input type="checkbox" id="forceCompression">
        <label for="forceCompression" class="check-label">Force compression (may reduce image quality)</label>
      </div>
    </div>

    <button class="btn" id="compressBtn" onclick="compressPdfFile()">Compress PDF</button>
    <div id="compressStatus" class="status"></div>
    <a id="compressDownload" class="download-btn" download>Download Compressed PDF</a>
  </div>

  <div class="panel right">
    <h2>Compression Result</h2>
    <div id="compressResult" class="empty">Compression details will appear here.</div>
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

<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
function switchTab(tab) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  event.target.classList.add('active');
}

const themeToggle = document.getElementById("themeToggle");
const themeIcon = document.getElementById("themeIcon");

function applyTheme(theme) {
  const isDark = theme === "dark";
  document.body.classList.toggle("dark-mode", isDark);
  themeIcon.textContent = isDark ? "☾" : "☀";
  themeToggle.setAttribute("aria-label", isDark ? "Switch to light mode" : "Switch to dark mode");
  themeToggle.setAttribute("title", isDark ? "Switch to light mode" : "Switch to dark mode");
}

function getSavedTheme() {
  const saved = localStorage.getItem("theme");
  return saved === "dark" ? "dark" : "light";
}

applyTheme(getSavedTheme());

themeToggle.addEventListener("click", () => {
  const nextTheme = document.body.classList.contains("dark-mode") ? "light" : "dark";
  applyTheme(nextTheme);
  localStorage.setItem("theme", nextTheme);
});

const dropZone = document.getElementById("dropZone");
const pdfInput = document.getElementById("pdfInput");
const fileName = document.getElementById("fileName");

dropZone.addEventListener("click", () => pdfInput.click());
pdfInput.addEventListener("change", () => {
  if (pdfInput.files[0]) {
    fileName.textContent = pdfInput.files[0].name;
  }
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
  const pages = (document.getElementById("pages").value || "").trim() || "all";
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
  g.innerHTML = images.map((src, i) =>
    `<img src="${src}" loading="lazy" alt="" style="animation-delay:${(i * 0.06).toFixed(2)}s" onclick="openLightbox('${src}')">`
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
    `<li style="animation-delay:${(i * 0.05).toFixed(2)}s"><span class="remove" onclick="removeImage(${i})">&times;</span> ${f.name}</li>`
  ).join("");
  document.getElementById("imgFileNames").textContent =
    imgFiles.length ? imgFiles.length + " image(s) selected" : "";
}

function renderImgPreview() {
  const g = document.getElementById("imgPreview");
  if (!imgFiles.length) { g.className = "empty"; g.innerHTML = "Selected images will appear here."; return; }
  g.className = "gallery";
  g.innerHTML = "";
  imgFiles.forEach((f, i) => {
    const img = document.createElement("img");
    img.src = URL.createObjectURL(f);
    img.loading = "lazy";
    img.style.animationDelay = `${(i * 0.06).toFixed(2)}s`;
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

const compressDropZone = document.getElementById("compressDropZone");
const compressInput = document.getElementById("compressInput");
const compressFileName = document.getElementById("compressFileName");
const targetReduction = document.getElementById("targetReduction");
const targetReductionValue = document.getElementById("targetReductionValue");
const forceCompression = document.getElementById("forceCompression");

const compressImgDropZone = document.getElementById("compressImgDropZone");
const compressImgInput = document.getElementById("compressImgInput");
const compressImgFileName = document.getElementById("compressImgFileName");
const imgTargetReduction = document.getElementById("imgTargetReduction");
const imgTargetReductionValue = document.getElementById("imgTargetReductionValue");
const imgForceCompression = document.getElementById("imgForceCompression");

const pdfEditDropZone = document.getElementById("pdfEditDropZone");
const pdfEditInput = document.getElementById("pdfEditInput");
const pdfEditFileNames = document.getElementById("pdfEditFileNames");
const pdfEditFileList = document.getElementById("pdfEditFileList");
const pdfEditOperationHelp = document.getElementById("pdfEditOperationHelp");
const pdfEditDropText = document.getElementById("pdfEditDropText");
const pdfEditBtn = document.getElementById("pdfEditBtn");
const pdfEditSideTitle = document.getElementById("pdfEditSideTitle");
const pdfMergeInfo = document.getElementById("pdfMergeInfo");
const pdfVisualField = document.getElementById("pdfVisualField");
const pdfVisualLoadBtn = document.getElementById("pdfVisualLoadBtn");
const pdfVisualGrid = document.getElementById("pdfVisualGrid");
let pdfEditFiles = [];
let pdfVisualPages = [];
let pdfVisualLoadedKey = "";

if (window.pdfjsLib && window.pdfjsLib.GlobalWorkerOptions) {
  window.pdfjsLib.GlobalWorkerOptions.workerSrc = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
}

targetReduction.addEventListener("input", () => {
  targetReductionValue.textContent = `${targetReduction.value}%`;
});

imgTargetReduction.addEventListener("input", () => {
  imgTargetReductionValue.textContent = `${imgTargetReduction.value}%`;
});

function isPdfFile(file) {
  return file && file.name && file.name.toLowerCase().endsWith(".pdf");
}

function renderPdfEditFiles() {
  pdfEditFileList.innerHTML = pdfEditFiles.map((file, index) => {
    const mergeMode = isPdfMergeMode();
    const itemClass = mergeMode ? "draggable-file" : "";
    const orderBadge = mergeMode ? `<span class="file-order-badge">${index + 1}</span>` : "";
    const dragAttr = mergeMode ? 'draggable="true"' : "";
    return `<li class="${itemClass}" data-index="${index}" ${dragAttr} style="animation-delay:${(index * 0.05).toFixed(2)}s"><span class="remove" onclick="removePdfEditFile(${index})">&times;</span>${orderBadge}<span>${file.name}</span></li>`;
  }).join("");
  pdfEditFileNames.textContent = pdfEditFiles.length ? `${pdfEditFiles.length} PDF file(s) selected` : "";

  if (!isPdfMergeMode()) return;

  let dragIndex = null;
  const clearDragTargets = () => {
    pdfEditFileList.querySelectorAll("li.drag-target").forEach((node) => node.classList.remove("drag-target"));
  };

  pdfEditFileList.querySelectorAll("li.draggable-file").forEach((item) => {
    item.addEventListener("dragstart", (event) => {
      dragIndex = Number(item.dataset.index);
      item.classList.add("dragging");
      event.dataTransfer.effectAllowed = "move";
    });
    item.addEventListener("dragend", () => {
      item.classList.remove("dragging");
      clearDragTargets();
      dragIndex = null;
    });
    item.addEventListener("dragenter", (event) => {
      event.preventDefault();
      const hoverIndex = Number(item.dataset.index);
      if (!Number.isFinite(dragIndex) || hoverIndex === dragIndex) return;
      clearDragTargets();
      item.classList.add("drag-target");
    });
    item.addEventListener("dragover", (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
    });
    item.addEventListener("dragleave", (event) => {
      if (!item.contains(event.relatedTarget)) {
        item.classList.remove("drag-target");
      }
    });
    item.addEventListener("drop", (event) => {
      event.preventDefault();
      clearDragTargets();
      const dropIndex = Number(item.dataset.index);
      if (!Number.isFinite(dragIndex) || dragIndex === dropIndex) return;
      const [moved] = pdfEditFiles.splice(dragIndex, 1);
      pdfEditFiles.splice(dropIndex, 0, moved);
      renderPdfEditFiles();
    });
  });
}

function getPdfEditFileKey(file) {
  return `${file.name}:${file.size}:${file.lastModified}`;
}

function normalizeRotation(value) {
  const normalized = ((value % 360) + 360) % 360;
  return [0, 90, 180, 270].includes(normalized) ? normalized : 0;
}

function isPdfMergeMode() {
  return pdfEditFiles.length > 1;
}

function renderPdfVisualGrid() {
  if (!pdfVisualPages.length) {
    pdfVisualGrid.innerHTML = "";
    return;
  }

  pdfVisualGrid.innerHTML = pdfVisualPages.map((page, index) => `
    <div class="pdf-page-card" draggable="true" data-index="${index}" style="animation-delay:${(index * 0.05).toFixed(2)}s">
      <img class="pdf-thumb" src="${page.thumb}" alt="Page ${page.page}">
      <div class="pdf-page-meta">
        <span>Page ${page.page}</span>
        <span>Slot ${index + 1}</span>
      </div>
      <button type="button" class="tick-btn ${page.include ? "on" : ""}" data-action="toggle-select" data-index="${index}">
        ${page.include ? "✓" : "❌"}
      </button>
      <div class="pdf-page-actions">
        <button type="button" class="mini-btn" data-action="rotate-left" data-index="${index}">Rotate -90</button>
        <button type="button" class="mini-btn" data-action="rotate-right" data-index="${index}">Rotate +90</button>
      </div>
      <div class="pdf-rotation">Rotation: ${page.rotation}&deg;</div>
    </div>
  `).join("");

  let dragIndex = null;
  const clearDragTarget = () => {
    pdfVisualGrid.querySelectorAll(".pdf-page-card.drag-target").forEach((node) => {
      node.classList.remove("drag-target");
    });
  };

  pdfVisualGrid.querySelectorAll(".pdf-page-card").forEach((card) => {
    card.addEventListener("dragstart", (e) => {
      dragIndex = Number(card.dataset.index);
      card.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
    });
    card.addEventListener("dragend", () => {
      card.classList.remove("dragging");
      clearDragTarget();
      dragIndex = null;
    });
    card.addEventListener("dragenter", (e) => {
      e.preventDefault();
      const hoverIndex = Number(card.dataset.index);
      if (!Number.isFinite(dragIndex) || hoverIndex === dragIndex) return;
      clearDragTarget();
      card.classList.add("drag-target");
    });
    card.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
    });
    card.addEventListener("dragleave", (e) => {
      if (!card.contains(e.relatedTarget)) {
        card.classList.remove("drag-target");
      }
    });
    card.addEventListener("drop", (e) => {
      e.preventDefault();
      clearDragTarget();
      const dropIndex = Number(card.dataset.index);
      if (!Number.isFinite(dragIndex) || dragIndex === dropIndex) return;
      const [moved] = pdfVisualPages.splice(dragIndex, 1);
      pdfVisualPages.splice(dropIndex, 0, moved);
      renderPdfVisualGrid();
    });
  });
}

async function loadPdfVisualEditor() {
  if (pdfEditFiles.length !== 1) {
    setStatus("pdfEditStatus", "Editor requires exactly one PDF file.", "error");
    return;
  }
  if (!window.pdfjsLib) {
    setStatus("pdfEditStatus", "PDF preview library failed to load.", "error");
    return;
  }

  const file = pdfEditFiles[0];
  const fileKey = getPdfEditFileKey(file);
  if (pdfVisualLoadedKey === fileKey && pdfVisualPages.length) {
    setStatus("pdfEditStatus", "Page editor is ready.", "ok");
    return;
  }

  pdfVisualLoadBtn.disabled = true;
  pdfVisualLoadBtn.textContent = "Loading pages...";
  setStatus("pdfEditStatus", "Loading page thumbnails...", "");

  try {
    const bytes = await file.arrayBuffer();
    const doc = await window.pdfjsLib.getDocument({ data: bytes }).promise;
    const pages = [];

    for (let pageNumber = 1; pageNumber <= doc.numPages; pageNumber += 1) {
      const page = await doc.getPage(pageNumber);
      const viewport = page.getViewport({ scale: 0.3 });
      const canvas = document.createElement("canvas");
      const context = canvas.getContext("2d", { alpha: false });
      canvas.width = Math.max(1, Math.floor(viewport.width));
      canvas.height = Math.max(1, Math.floor(viewport.height));
      await page.render({ canvasContext: context, viewport }).promise;

      pages.push({
        page: pageNumber,
        include: true,
        rotation: 0,
        thumb: canvas.toDataURL("image/jpeg", 0.82),
      });
    }

    pdfVisualPages = pages;
    pdfVisualLoadedKey = fileKey;
    renderPdfVisualGrid();
    setStatus("pdfEditStatus", `Loaded ${pages.length} page thumbnail(s).`, "ok");
  } catch (e) {
    setStatus("pdfEditStatus", "Could not load PDF preview: " + e.message, "error");
  } finally {
    pdfVisualLoadBtn.disabled = false;
    pdfVisualLoadBtn.textContent = "Load Page Thumbnails";
  }
}

pdfVisualGrid.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;

  const index = Number(target.dataset.index);
  if (!Number.isFinite(index) || !pdfVisualPages[index]) return;

  const action = target.dataset.action;
  if (action === "toggle-select") {
    pdfVisualPages[index].include = !pdfVisualPages[index].include;
    renderPdfVisualGrid();
  }
  if (action === "rotate-left") {
    pdfVisualPages[index].rotation = normalizeRotation(pdfVisualPages[index].rotation - 90);
    renderPdfVisualGrid();
  }
  if (action === "rotate-right") {
    pdfVisualPages[index].rotation = normalizeRotation(pdfVisualPages[index].rotation + 90);
    renderPdfVisualGrid();
  }
});

function addPdfEditFiles(fileList) {
  for (const file of fileList) {
    if (isPdfFile(file)) {
      pdfEditFiles.push(file);
    }
  }
  renderPdfEditFiles();
  if (pdfEditFiles.length !== 1 || getPdfEditFileKey(pdfEditFiles[0]) !== pdfVisualLoadedKey) {
    pdfVisualPages = [];
    pdfVisualLoadedKey = "";
    renderPdfVisualGrid();
  }
  updatePdfEditUI();
}

function removePdfEditFile(index) {
  pdfEditFiles.splice(index, 1);
  renderPdfEditFiles();
  if (pdfEditFiles.length !== 1 || getPdfEditFileKey(pdfEditFiles[0]) !== pdfVisualLoadedKey) {
    pdfVisualPages = [];
    pdfVisualLoadedKey = "";
    renderPdfVisualGrid();
  }
  updatePdfEditUI();
}

function updatePdfEditUI() {
  if (isPdfMergeMode()) {
    pdfEditOperationHelp.textContent = "Add two or more PDFs to merge them into one file. Drag the selected files to change the merge order.";
    pdfEditDropText.textContent = "Click or drag PDF files here";
    pdfEditBtn.textContent = "Merge PDFs";
    document.getElementById("pdfEditDownload").textContent = "Download Merged PDF";
    pdfEditSideTitle.textContent = "Merge Preview";
    pdfMergeInfo.classList.remove("hidden");
    pdfVisualField.classList.add("hidden");
    return;
  }

  pdfEditOperationHelp.textContent = "Load one PDF and drag cards to reorder pages. Use tick and rotate controls on each page card.";
  pdfEditDropText.textContent = "Click or drag a PDF here";
  pdfEditBtn.textContent = "Apply Page Edit";
  document.getElementById("pdfEditDownload").textContent = "Download Edited PDF";
  pdfEditSideTitle.textContent = "Page Thumbnails";
  pdfMergeInfo.classList.add("hidden");
  pdfVisualField.classList.remove("hidden");
}

pdfEditDropZone.addEventListener("click", () => pdfEditInput.click());
pdfEditInput.addEventListener("change", () => addPdfEditFiles(pdfEditInput.files));
pdfEditDropZone.addEventListener("dragover", e => { e.preventDefault(); pdfEditDropZone.classList.add("dragover"); });
pdfEditDropZone.addEventListener("dragleave", () => pdfEditDropZone.classList.remove("dragover"));
pdfEditDropZone.addEventListener("drop", e => {
  e.preventDefault();
  pdfEditDropZone.classList.remove("dragover");
  addPdfEditFiles(e.dataTransfer.files);
});
pdfVisualLoadBtn.addEventListener("click", loadPdfVisualEditor);
updatePdfEditUI();

compressDropZone.addEventListener("click", () => compressInput.click());
compressInput.addEventListener("change", () => {
  if (compressInput.files[0]) compressFileName.textContent = compressInput.files[0].name;
});
compressDropZone.addEventListener("dragover", e => { e.preventDefault(); compressDropZone.classList.add("dragover"); });
compressDropZone.addEventListener("dragleave", () => compressDropZone.classList.remove("dragover"));
compressDropZone.addEventListener("drop", e => {
  e.preventDefault();
  compressDropZone.classList.remove("dragover");
  const f = e.dataTransfer.files[0];
  if (f && f.type === "application/pdf") {
    const dt = new DataTransfer(); dt.items.add(f);
    compressInput.files = dt.files;
    compressFileName.textContent = f.name;
  }
});

compressImgDropZone.addEventListener("click", () => compressImgInput.click());
compressImgInput.addEventListener("change", () => {
  if (compressImgInput.files[0]) compressImgFileName.textContent = compressImgInput.files[0].name;
});
compressImgDropZone.addEventListener("dragover", e => { e.preventDefault(); compressImgDropZone.classList.add("dragover"); });
compressImgDropZone.addEventListener("dragleave", () => compressImgDropZone.classList.remove("dragover"));
compressImgDropZone.addEventListener("drop", e => {
  e.preventDefault();
  compressImgDropZone.classList.remove("dragover");
  const f = e.dataTransfer.files[0];
  if (f && f.type.startsWith("image/")) {
    const dt = new DataTransfer(); dt.items.add(f);
    compressImgInput.files = dt.files;
    compressImgFileName.textContent = f.name;
  }
});

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const idx = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / (1024 ** idx)).toFixed(idx === 0 ? 0 : 2)} ${units[idx]}`;
}

async function compressPdfFile() {
  const file = compressInput.files[0];
  if (!file) { setStatus("compressStatus", "Please select a PDF file.", "error"); return; }

  const targetPercent = Number(targetReduction.value || 30);
  const btn = document.getElementById("compressBtn");

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Compressing...';
  setStatus("compressStatus", "Uploading and compressing...", "");

  const form = new FormData();
  form.append("pdf", file);
  form.append("target_percent", String(targetPercent));
  form.append("force_compression", forceCompression.checked ? "true" : "false");

  try {
    const res = await fetch("/compress-pdf", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) { setStatus("compressStatus", data.error || "Compression failed.", "error"); return; }

    const hitTargetText = data.achieved_target ? "Target met" : "Best possible for this file";
    const forceText = data.forced_used ? " Forced mode applied." : "";
    setStatus("compressStatus", `Compression complete. ${hitTargetText}.`, "ok");
    const result = document.getElementById("compressResult");
    result.className = "";
    result.innerHTML = `
      <p><strong>Target:</strong> ${data.target_percent}%</p>
      <p><strong>Original:</strong> ${formatBytes(data.original_bytes)}</p>
      <p><strong>Compressed:</strong> ${formatBytes(data.compressed_bytes)}</p>
      <p><strong>Reduction:</strong> ${data.reduction_percent}%</p>
    `;
    result.style.animation = "none";
    void result.offsetHeight;
    result.style.animation = "rise .4s ease-out";

    if (forceText) {
      setStatus("compressStatus", `Compression complete. ${hitTargetText}.${forceText}`, "ok");
    }

    const dl = document.getElementById("compressDownload");
    dl.dataset.url = data.pdf;
    dl.dataset.name = data.pdf.split('/').pop() || "compressed.pdf";
    dl.style.display = "block";
  } catch (e) {
    setStatus("compressStatus", "Network error: " + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Compress PDF";
  }
}

async function compressImageFile() {
  const file = compressImgInput.files[0];
  if (!file) { setStatus("imgCompressStatus", "Please select an image file.", "error"); return; }

  const targetPercent = Number(imgTargetReduction.value || 30);
  const btn = document.getElementById("imgCompressBtn");

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Compressing...';
  setStatus("imgCompressStatus", "Uploading and compressing...", "");

  const form = new FormData();
  form.append("image", file);
  form.append("target_percent", String(targetPercent));
  form.append("force_compression", imgForceCompression.checked ? "true" : "false");

  try {
    const res = await fetch("/compress-image", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) { setStatus("imgCompressStatus", data.error || "Compression failed.", "error"); return; }

    const hitTargetText = data.achieved_target ? "Target met" : "Best possible for this file";
    const forceText = data.forced_used ? " Forced mode applied." : "";
    setStatus("imgCompressStatus", `Compression complete. ${hitTargetText}.`, "ok");

    const result = document.getElementById("imgCompressResult");
    result.className = "";
    result.innerHTML = `
      <p><strong>Target:</strong> ${data.target_percent}%</p>
      <p><strong>Original:</strong> ${formatBytes(data.original_bytes)}</p>
      <p><strong>Compressed:</strong> ${formatBytes(data.compressed_bytes)}</p>
      <p><strong>Reduction:</strong> ${data.reduction_percent}%</p>
    `;
    result.style.animation = "none";
    void result.offsetHeight;
    result.style.animation = "rise .4s ease-out";

    if (forceText) {
      setStatus("imgCompressStatus", `Compression complete. ${hitTargetText}.${forceText}`, "ok");
    }

    const dl = document.getElementById("imgCompressDownload");
    dl.dataset.url = data.image;
    dl.dataset.name = data.image.split('/').pop() || "compressed-image.jpg";
    dl.style.display = "block";
  } catch (e) {
    setStatus("imgCompressStatus", "Network error: " + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Compress Image";
  }
}

async function modifyPdf() {
  const btn = pdfEditBtn;

  if (!pdfEditFiles.length) {
    setStatus("pdfEditStatus", "Please select at least one PDF file.", "error");
    return;
  }

  if (isPdfMergeMode()) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>Merging...';
    setStatus("pdfEditStatus", "Uploading and merging PDFs...", "");

    const form = new FormData();
    form.append("operation", "merge");
    pdfEditFiles.forEach((file) => form.append("pdfs", file));

    try {
      const res = await fetch("/modify-pdf", { method: "POST", body: form });
      const data = await res.json();
      if (!res.ok) {
        setStatus("pdfEditStatus", data.error || "PDF merge failed.", "error");
        return;
      }

      setStatus("pdfEditStatus", data.summary || "PDFs merged successfully.", "ok");

      const dl = document.getElementById("pdfEditDownload");
      dl.dataset.url = data.file;
      dl.dataset.name = data.filename || "merged-document.pdf";
      dl.style.display = "block";
    } catch (e) {
      setStatus("pdfEditStatus", "Network error: " + e.message, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Merge PDFs";
    }
    return;
  }

  const operation = "visual_edit";

  if (pdfEditFiles.length !== 1) {
    setStatus("pdfEditStatus", "This operation requires exactly one PDF file.", "error");
    return;
  }

  await loadPdfVisualEditor();
  if (!pdfVisualPages.length) {
    return;
  }

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Processing...';
  setStatus("pdfEditStatus", "Uploading and modifying PDF...", "");

  const form = new FormData();
  form.append("operation", operation);
  form.append("pdf", pdfEditFiles[0]);

  const selectedSlots = pdfVisualPages
    .filter(page => page.include)
    .map(page => ({ page: page.page, rotation: normalizeRotation(page.rotation) }));
  if (!selectedSlots.length) {
    btn.disabled = false;
    btn.textContent = "Apply Page Edit";
    setStatus("pdfEditStatus", "Select at least one page to keep in the output.", "error");
    return;
  }
  form.append("slots_json", JSON.stringify(selectedSlots));

  try {
    const res = await fetch("/modify-pdf", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) {
      setStatus("pdfEditStatus", data.error || "PDF edit failed.", "error");
      return;
    }

    setStatus("pdfEditStatus", data.summary || "PDF updated successfully.", "ok");

    const dl = document.getElementById("pdfEditDownload");
    dl.dataset.url = data.file;
    dl.dataset.name = data.filename || "edited.pdf";
    dl.style.display = "block";
  } catch (e) {
    setStatus("pdfEditStatus", "Network error: " + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Apply Page Edit";
  }
}

function setStatus(id, msg, cls) {
  const el = document.getElementById(id);
  el.className = "status " + cls;
  el.textContent = msg;
  if (msg) {
    el.style.animation = "none";
    void el.offsetHeight;
    el.style.animation = "";
  }
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

document.getElementById("compressDownload").addEventListener("click", (e) => {
  e.preventDefault();
  downloadFromEndpoint("compressDownload", "compressStatus");
});

document.getElementById("pdfEditDownload").addEventListener("click", (e) => {
  e.preventDefault();
  downloadFromEndpoint("pdfEditDownload", "pdfEditStatus");
});

document.getElementById("imgCompressDownload").addEventListener("click", (e) => {
  e.preventDefault();
  downloadFromEndpoint("imgCompressDownload", "imgCompressStatus");
});

document.addEventListener("DOMContentLoaded", () => {
  const donateBtn = document.getElementById("donateBtn");
  const donateModal = document.getElementById("donateModal");
  const donateCancel = document.getElementById("donateCancel");
  const donateConfirm = document.getElementById("donateConfirm");
  const donateAmount = document.getElementById("donateAmount");
  if (!donateBtn || !donateModal || !donateConfirm) return;

  function openDonateModal() {
    donateModal.classList.add("open");
    if (donateAmount) donateAmount.focus();
  }

  function closeDonateModal() {
    donateModal.classList.remove("open");
  }

  donateBtn.addEventListener("click", openDonateModal);
  if (donateCancel) donateCancel.addEventListener("click", closeDonateModal);

  donateModal.addEventListener("click", (event) => {
    if (event.target === donateModal) closeDonateModal();
  });

  donateConfirm.addEventListener("click", async () => {
    const rupees = donateAmount ? Number(donateAmount.value) : 0;
    if (!Number.isFinite(rupees) || rupees <= 0) {
      alert("Please enter a valid donation amount.");
      if (donateAmount) donateAmount.focus();
      return;
    }

    const amountPaise = Math.round(rupees * 100);
    if (amountPaise < 100) {
      alert("Minimum donation is Rs 1.");
      if (donateAmount) donateAmount.focus();
      return;
    }

    const originalText = donateConfirm.textContent;
    donateConfirm.disabled = true;
    donateConfirm.textContent = "Opening checkout...";

    try {
      const res = await fetch("/donate/order", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount: amountPaise })
      });
      const payload = await res.json();

      if (!res.ok) {
        throw new Error(payload.error || "Could not start payment.");
      }

      if (typeof window.Razorpay !== "function") {
        throw new Error("Razorpay Checkout failed to load.");
      }

      const options = {
        key: payload.key,
        amount: payload.amount,
        currency: payload.currency,
        name: payload.name,
        description: payload.description,
        order_id: payload.order_id,
        handler: function () {
          alert("Thank you for your support!");
          donateConfirm.disabled = false;
          donateConfirm.textContent = originalText;
          closeDonateModal();
        },
        modal: {
          ondismiss: function () {
            donateConfirm.disabled = false;
            donateConfirm.textContent = originalText;
          }
        },
        theme: { color: "#FC8019" }
      };

      const razorpayCheckout = new Razorpay(options);
      razorpayCheckout.on("payment.failed", function (response) {
        const reason = response && response.error ? response.error.description : "Payment failed.";
        alert(reason);
        donateConfirm.disabled = false;
        donateConfirm.textContent = originalText;
      });
      razorpayCheckout.open();
    } catch (err) {
      alert(err.message || "Unable to open checkout.");
      donateConfirm.disabled = false;
      donateConfirm.textContent = originalText;
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && donateModal.classList.contains("open")) {
      closeDonateModal();
    }
  });
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
<div class="donate-bar">
  <button type="button" id="donateBtn" class="donate-btn">Donate</button>
</div>
<div id="donateModal" class="donate-modal" role="dialog" aria-modal="true" aria-labelledby="donateTitle">
  <div class="donate-modal-card">
    <h3 id="donateTitle">Support Swift Convert</h3>
    <p>Enter any donation amount in rupees.</p>
    <input type="number" id="donateAmount" class="donate-amount" min="1" step="1" value="101" aria-label="Donation amount in rupees" placeholder="Amount (Rs)">
    <div class="donate-actions">
      <button type="button" id="donateConfirm" class="donate-btn">Continue to Pay</button>
      <button type="button" id="donateCancel" class="donate-btn alt">Cancel</button>
    </div>
  </div>
</div>
</body>
</html>
"""

if __name__ == "__main__":
  print("Starting PDF to Image Converter...")
  print("Open http://127.0.0.1:5001 in your browser")
  app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)
