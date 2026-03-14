import json
import os
import re
import shutil
import sys
import zipfile

import fitz
from PIL import Image


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


def parse_page_range(page_range_str: str, total_pages: int) -> list:
    s = (page_range_str or "").strip().lower()
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
                raise ValueError(f"Page {n} out of bounds (doc has {total_pages} pages)")
            indices.add(n - 1)
    return sorted(indices)


def cmd_convert(payload):
    input_path = payload["input_path"]
    tmp_dir = payload["tmp_dir"]
    fmt = str(payload.get("format", "PNG")).upper()
    dpi = int(payload.get("dpi", 150))
    pages_str = str(payload.get("pages", "all"))

    if fmt not in ("PNG", "JPEG"):
        raise ValueError("Format must be PNG or JPEG.")
    if dpi not in (72, 150, 300):
        raise ValueError("DPI must be 72, 150, or 300.")

    zip_name = "converted_images.zip"
    zip_path = os.path.join(tmp_dir, zip_name)

    image_names = []
    doc = fitz.open(input_path)
    try:
        total = len(doc)
        pages = parse_page_range(pages_str, total)
        if not pages:
            raise ValueError("No pages selected.")

        scale = dpi / 72.0
        matrix = fitz.Matrix(scale, scale)
        ext = "jpg" if fmt == "JPEG" else "png"

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as archive:
            for idx in pages:
                page = doc[idx]
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                name = f"page_{idx + 1:04d}.{ext}"
                out_path = os.path.join(tmp_dir, name)

                if fmt == "JPEG":
                    pix.save(out_path, jpg_quality=92)
                else:
                    pix.save(out_path)

                image_names.append(name)
                archive.write(out_path, arcname=name)

        return {
            "count": len(pages),
            "total_pages": total,
            "image_names": image_names,
            "zip_name": zip_name,
        }
    finally:
        doc.close()


def cmd_img2pdf(payload):
    image_paths = payload.get("image_paths", [])
    page_size = str(payload.get("page_size", "A4")).upper()
    orientation = str(payload.get("orientation", "portrait")).lower()
    tmp_dir = payload["tmp_dir"]

    sizes = {
        "A4": (595.28, 841.89),
        "LETTER": (612, 792),
        "A3": (841.89, 1190.55),
    }
    if page_size not in sizes:
        raise ValueError("Page size must be A4, Letter, or A3.")
    if orientation not in ("portrait", "landscape"):
        raise ValueError("Orientation must be portrait or landscape.")

    w, h = sizes[page_size]
    if orientation == "landscape":
        w, h = h, w

    doc = fitz.open()
    valid_inputs = []
    try:
        for path in image_paths:
            ext = os.path.splitext(path)[1].lower()
            if ext not in ALLOWED_IMAGE_EXTENSIONS:
                continue
            valid_inputs.append(path)

            img = Image.open(path)
            img_w, img_h = img.size
            img.close()

            page = doc.new_page(width=w, height=h)
            iw, ih = float(img_w), float(img_h)
            scale = min(w / iw, h / ih)
            rw, rh = iw * scale, ih * scale
            x0 = (w - rw) / 2
            y0 = (h - rh) / 2
            rect = fitz.Rect(x0, y0, x0 + rw, y0 + rh)
            page.insert_image(rect, filename=path)

        if len(doc) == 0:
            raise ValueError("No valid image files found.")

        first_name = os.path.splitext(os.path.basename(valid_inputs[0]))[0] if valid_inputs else "images"
        pdf_name = f"converted-{first_name}.pdf"
        out_path = os.path.join(tmp_dir, pdf_name)
        doc.save(out_path)
        return {"pdf_name": pdf_name, "pages": len(valid_inputs)}
    finally:
        doc.close()


def cmd_compress_pdf(payload):
    input_path = payload["input_path"]
    tmp_dir = payload["tmp_dir"]
    target_percent = float(payload.get("target_percent", 30))
    force_compression = bool(payload.get("force_compression", False))
    original_filename = str(payload.get("original_filename", "document.pdf"))

    if target_percent < 1 or target_percent > 90:
        raise ValueError("Target percent must be between 1 and 90.")

    original_size = os.path.getsize(input_path)

    probe_doc = fitz.open(input_path)
    try:
        if probe_doc.needs_pass:
            raise ValueError("Password-protected PDFs are not supported.")
    finally:
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
        try:
            if profile["subset_fonts"] and hasattr(doc, "subset_fonts"):
                doc.subset_fonts()
            doc.save(candidate_path, **profile["save_options"])
        finally:
            doc.close()

        compressed_size = os.path.getsize(candidate_path)
        reduction = 0.0 if original_size <= 0 else (1 - (compressed_size / original_size)) * 100

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
            reduction = 0.0 if original_size <= 0 else (1 - (compressed_size / original_size)) * 100

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

    base = os.path.splitext(os.path.basename(original_filename))[0] or "document"
    out_name = f"compressed-{base}.pdf"
    out_path = os.path.join(tmp_dir, out_name)
    shutil.copyfile(selected_attempt["path"], out_path)

    return {
        "pdf_name": out_name,
        "original_bytes": original_size,
        "compressed_bytes": selected_attempt["size"],
        "reduction_percent": round(selected_attempt["reduction"], 2),
        "target_percent": round(target_percent, 2),
        "achieved_target": selected_attempt["reduction"] >= target_percent,
        "profile": selected_attempt["profile"],
        "force_requested": force_compression,
        "forced_used": forced_used,
    }


def cmd_compress_image(payload):
    input_path = payload["input_path"]
    tmp_dir = payload["tmp_dir"]
    target_percent = float(payload.get("target_percent", 30))
    force_compression = bool(payload.get("force_compression", False))
    original_filename = str(payload.get("original_filename", "image"))

    if target_percent < 1 or target_percent > 90:
        raise ValueError("Target percent must be between 1 and 90.")

    ext = os.path.splitext(input_path)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("Unsupported image format.")

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
            reduction = 0.0 if original_size <= 0 else (1 - (compressed_size / original_size)) * 100

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
                reduction = 0.0 if original_size <= 0 else (1 - (compressed_size / original_size)) * 100

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

    base = os.path.splitext(os.path.basename(original_filename))[0] or "image"
    out_name = f"compressed-{base}.{selected_attempt['ext']}"
    out_path = os.path.join(tmp_dir, out_name)
    shutil.copyfile(selected_attempt["path"], out_path)

    return {
        "image_name": out_name,
        "original_bytes": original_size,
        "compressed_bytes": selected_attempt["size"],
        "reduction_percent": round(selected_attempt["reduction"], 2),
        "target_percent": round(target_percent, 2),
        "achieved_target": selected_attempt["reduction"] >= target_percent,
        "force_requested": force_compression,
        "forced_used": forced_used,
    }


def cmd_modify_pdf(payload):
    operation = str(payload.get("operation", "")).strip().lower()
    if operation not in {"merge", "visual_edit"}:
        raise ValueError("Invalid PDF edit operation.")

    tmp_dir = payload["tmp_dir"]

    if operation == "merge":
        input_paths = payload.get("input_paths", [])
        if len(input_paths) < 2:
            raise ValueError("Merge requires at least two PDF files.")

        merged_doc = fitz.open()
        merged_pages = 0
        try:
            for path in input_paths:
                source_doc = fitz.open(path)
                try:
                    if source_doc.needs_pass:
                        raise ValueError("Password-protected PDFs are not supported.")
                    merged_doc.insert_pdf(source_doc)
                    merged_pages += len(source_doc)
                finally:
                    source_doc.close()

            out_name = "merged-document.pdf"
            out_path = os.path.join(tmp_dir, out_name)
            merged_doc.save(out_path)
        finally:
            merged_doc.close()

        return {
            "filename": out_name,
            "page_count": merged_pages,
            "output_count": 1,
            "summary": f"Merged {len(input_paths)} PDFs into one file.",
        }

    input_path = payload.get("input_path")
    if not input_path:
        raise ValueError("This operation requires exactly one PDF file.")

    source_doc = fitz.open(input_path)
    try:
        if source_doc.needs_pass:
            raise ValueError("Password-protected PDFs are not supported.")

        total_pages = len(source_doc)
        base_name = os.path.splitext(os.path.basename(input_path))[0] or "document"

        slots = payload.get("slots", [])
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
                    raise ValueError("Rotation must be one of 0, 90, 180, or 270.")

                edited_doc.insert_pdf(
                    source_doc,
                    from_page=page_number - 1,
                    to_page=page_number - 1,
                )
                edited_doc[-1].set_rotation(rotation)

            out_name = f"edited-{base_name}.pdf"
            out_path = os.path.join(tmp_dir, out_name)
            edited_doc.save(out_path)
        finally:
            edited_doc.close()

        return {
            "filename": out_name,
            "page_count": len(slots),
            "output_count": 1,
            "summary": f"Applied page edits to {len(slots)} slot(s).",
        }
    finally:
        source_doc.close()


COMMANDS = {
    "convert": cmd_convert,
    "img2pdf": cmd_img2pdf,
    "compress_pdf": cmd_compress_pdf,
    "compress_image": cmd_compress_image,
    "modify_pdf": cmd_modify_pdf,
}


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"ok": False, "error": "No input payload provided."}))
        return

    try:
        req = json.loads(raw)
        command = req.get("command")
        payload = req.get("payload", {})

        if command not in COMMANDS:
            raise ValueError("Unknown command.")

        result = COMMANDS[command](payload)
        print(json.dumps({"ok": True, "result": result}))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))


if __name__ == "__main__":
    main()
