"""Generate A4 PDF sheets from paired head/body SVG orders.

This script scans an input directory for order SVGs that follow the
``<order>_head.svg`` and ``<order>_body.svg`` naming pattern. For each
order it combines the two SVGs into a single ``figabooth`` SVG where the
head is placed above the body, centres them horizontally, and writes the
result into an output directory. Finally it lays all generated
``figabooth`` SVGs out on one or more A4 PDF pages.
"""
from __future__ import annotations

import argparse
import copy
import io
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET
import re
from functools import lru_cache

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPDF
    from PyPDF2 import PdfReader, PdfWriter
    import cairosvg
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from fontTools.ttLib import TTFont
    from fontTools.pens.svgPathPen import SVGPathPen
except ImportError as exc:  # pragma: no cover - runtime guard
    raise SystemExit(
        "Missing dependencies. Please install them with 'pip install reportlab svglib PyPDF2 cairosvg pillow fonttools'."
    ) from exc

SVG_NS = "http://www.w3.org/2000/svg"
STYLE_TAG = f"{{{SVG_NS}}}style"
DEFS_TAG = f"{{{SVG_NS}}}defs"
PX_TO_MM = 0.2645833333
BACKGROUND_PDF = Path(__file__).resolve().with_name("background.pdf")


def parse_length(value: str | None) -> float:
    if value is None:
        raise ValueError("Missing SVG length attribute")
    value = value.strip()
    for suffix in ("px", "mm", "cm", "in"):
        if value.endswith(suffix):
            num = float(value[: -len(suffix)])
            if suffix == "px":
                return num
            if suffix == "mm":
                return num / PX_TO_MM
            if suffix == "cm":
                return (num * 10.0) / PX_TO_MM
            if suffix == "in":
                return (num * 25.4) / PX_TO_MM
    return float(value)


def parse_style(style_value: str | None) -> dict:
    if not style_value:
        return {}
    parts = [part.strip() for part in style_value.split(";") if part.strip()]
    style = {}
    for part in parts:
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        style[key.strip()] = value.strip()
    return style


def clean_font_family(value: str | None) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    if value.startswith(("'", '"')) and value.endswith(("'", '"')):
        value = value[1:-1]
    return value


@lru_cache(maxsize=8)
def load_font(font_path: str) -> TTFont:
    return TTFont(font_path)


def get_font_family_name(font: TTFont) -> Optional[str]:
    name_table = font["name"]
    for record in name_table.names:
        if record.nameID != 1:
            continue
        try:
            value = record.toUnicode().strip()
        except Exception:
            continue
        if value:
            return value
    return None


@lru_cache(maxsize=2)
def get_project_font_map(font_dir: str) -> dict[str, Path]:
    base = Path(font_dir)
    if not base.exists():
        return {}
    font_map: dict[str, Path] = {}
    for font_path in base.glob("*.ttf"):
        try:
            font = load_font(str(font_path))
        except Exception:
            continue
        family = get_font_family_name(font)
        if not family:
            continue
        font_map[family] = font_path
    return font_map


def get_kern_table(font: TTFont) -> dict:
    kern_table = {}
    if "kern" not in font:
        return kern_table
    for subtable in font["kern"].kernTables:
        if subtable.version != 0 or not getattr(subtable, "kernTable", None):
            continue
        for (left, right), value in subtable.kernTable.items():
            kern_table[(left, right)] = value
    return kern_table


def convert_text_to_paths(svg_text: str, font_family_paths: dict[str, Path]) -> str:
    if not font_family_paths:
        return svg_text
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError:
        return svg_text

    ns = {"svg": SVG_NS}
    text_elements = []

    for parent in root.iter():
        for child in list(parent):
            if child.tag == f"{{{SVG_NS}}}text":
                text_elements.append((parent, child))

    if not text_elements:
        return svg_text

    for parent, text_elem in text_elements:
        if list(text_elem):
            continue

        style = parse_style(text_elem.get("style"))
        font_family = clean_font_family(
            text_elem.get("font-family") or style.get("font-family")
        )
        if not font_family:
            continue

        font_path = font_family_paths.get(font_family)
        if not font_path or not font_path.exists():
            continue

        try:
            font = load_font(str(font_path))
        except Exception:
            continue

        text_value = text_elem.text or ""
        if not text_value.strip():
            continue

        font_size_value = text_elem.get("font-size") or style.get("font-size") or "16"
        try:
            font_size = parse_length(font_size_value)
        except ValueError:
            font_size = 16.0

        try:
            x = float(text_elem.get("x") or "0")
            y = float(text_elem.get("y") or "0")
        except ValueError:
            x, y = 0.0, 0.0

        text_anchor = (text_elem.get("text-anchor") or style.get("text-anchor") or "start").strip()
        dominant_baseline = (
            text_elem.get("dominant-baseline") or style.get("dominant-baseline") or ""
        ).strip()

        cmap = font.getBestCmap() or {}
        glyph_set = font.getGlyphSet()
        hmtx = font["hmtx"].metrics
        kern_table = get_kern_table(font)
        units_per_em = font["head"].unitsPerEm
        ascent = font["hhea"].ascent
        descent = font["hhea"].descent

        glyph_names = []
        for char in text_value:
            glyph_name = cmap.get(ord(char), ".notdef")
            glyph_names.append(glyph_name)

        advances = []
        total_advance = 0
        prev_glyph = None
        for glyph_name in glyph_names:
            advance, _ = hmtx.get(glyph_name, (0, 0))
            kern = 0
            if prev_glyph:
                kern = kern_table.get((prev_glyph, glyph_name), 0)
            total_advance += advance + kern
            advances.append((advance, kern))
            prev_glyph = glyph_name

        scale = font_size / units_per_em
        total_width = total_advance * scale
        if text_anchor in ("middle", "center"):
            start_x = x - total_width / 2.0
        elif text_anchor in ("end", "right"):
            start_x = x - total_width
        else:
            start_x = x

        if dominant_baseline == "middle":
            baseline_y = y - ((ascent + descent) / 2.0) * scale
        else:
            baseline_y = y

        group = ET.Element(f"{{{SVG_NS}}}g")
        fill = text_elem.get("fill") or style.get("fill")
        if fill:
            group.set("fill", fill)
        if "opacity" in text_elem.attrib:
            group.set("opacity", text_elem.get("opacity"))

        cursor = 0.0
        for glyph_name, (advance, kern) in zip(glyph_names, advances):
            glyph = glyph_set.get(glyph_name)
            if glyph is None:
                cursor += (advance + kern) * scale
                continue
            pen = SVGPathPen(glyph_set)
            glyph.draw(pen)
            d = pen.getCommands()
            if d:
                path = ET.Element(f"{{{SVG_NS}}}path", {"d": d})
                x_pos = start_x + cursor
                path.set("transform", f"translate({x_pos},{baseline_y}) scale({scale}, {-scale})")
                group.append(path)
            cursor += (advance + kern) * scale

        parent_index = list(parent).index(text_elem)
        parent.remove(text_elem)
        parent.insert(parent_index, group)

    return ET.tostring(root, encoding="unicode")


def element_dimensions(root: ET.Element) -> Tuple[float, float]:
    width = root.get("width")
    height = root.get("height")
    if width and height:
        return parse_length(width), parse_length(height)
    view_box = root.get("viewBox")
    if view_box:
        parts = view_box.replace(',', ' ').split()
        if len(parts) == 4:
            return float(parts[2]), float(parts[3])
    raise ValueError("SVG element must declare width/height or viewBox")


def figabooth_dimensions(head_root: ET.Element, body_root: ET.Element) -> Tuple[float, float, float, float, float, float]:
    head_width, head_height = element_dimensions(head_root)
    body_width, body_height = element_dimensions(body_root)
    total_width = max(head_width, body_width)
    total_height = head_height + body_height
    return head_width, head_height, body_width, body_height, total_width, total_height


def clone_as_group(element: ET.Element) -> ET.Element:
    group = copy.deepcopy(element)
    group.tag = f"{{{SVG_NS}}}g"
    for attr in ["width", "height", "viewBox", "x", "y"]:
        group.attrib.pop(attr, None)
    return group


def extract_defs_and_styles(*roots: ET.Element) -> List[ET.Element]:
    extracted: List[ET.Element] = []
    for root in roots:
        for child in list(root):
            if child.tag in (STYLE_TAG, DEFS_TAG):
                extracted.append(copy.deepcopy(child))
    return extracted


def strip_defs_and_styles(group: ET.Element) -> None:
    for child in list(group):
        if child.tag in (STYLE_TAG, DEFS_TAG):
            group.remove(child)


def decode_svg_bytes(data: bytes, source: Path) -> str:
    xml_header = data[:200].decode("ascii", errors="ignore")
    match = re.search(r'encoding=["\']([^"\']+)["\']', xml_header)
    encodings: List[str] = []
    if match:
        encodings.append(match.group(1))
    encodings.extend(["utf-8", "utf-8-sig", "big5", "utf-16", "utf-16le", "utf-16be"])
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    logging.warning("Failed to decode %s with declared encoding; using utf-8 replacement.", source)
    return data.decode("utf-8", errors="replace")


def parse_svg_root(svg_path: Path) -> ET.Element:
    svg_text = decode_svg_bytes(svg_path.read_bytes(), svg_path)
    return ET.fromstring(svg_text)


@dataclass
class Figabooth:
    order_id: str
    svg_path: Path
    width_px: float
    height_px: float


@dataclass
class OrderEntry:
    order_id: str
    head_path: Path
    torso_path: Path
    torso_back_path: Optional[Path] = None


def combine_order(head_path: Path, body_path: Path, output_path: Path) -> Figabooth:
    head_root = parse_svg_root(head_path)
    body_root = parse_svg_root(body_path)

    (head_width, head_height, body_width, body_height, total_width, total_height) = (
        figabooth_dimensions(head_root, body_root)
    )

    total_width = 57.2416
    total_height = 81.6532

    ET.register_namespace("", SVG_NS)
    root = ET.Element(
        f"{{{SVG_NS}}}svg",
        attrib={
            "width": f"{total_width}",
            "height": f"{total_height}",
            "viewBox": f"0 0 {total_width} {total_height}",
            "version": "1.1",
        },
    )

    for asset in extract_defs_and_styles(head_root, body_root):
        root.append(asset)

    head_group = clone_as_group(head_root)
    strip_defs_and_styles(head_group)
    head_offset_x = (total_width - head_width) / 2
    head_group.set("transform", f"translate({head_offset_x},0)")
    root.append(head_group)

    body_group = clone_as_group(body_root)
    strip_defs_and_styles(body_group)
    body_offset_x = (total_width - body_width) / 2
    #body_group.set("transform", f"translate({body_offset_x},{head_height})")
    body_offset_y = parse_length("8.254px")
    print(body_offset_y)
    body_group.set("transform", f"translate({body_offset_x},{head_height-body_offset_y})")
    root.append(body_group)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(output_path, encoding="utf-8", xml_declaration=True)

    return Figabooth(
        order_id=output_path.stem.replace("_figabooth", ""),
        svg_path=output_path,
        width_px=57.2416,
        height_px=81.6532,
        #width_px=total_width,
        #height_px=total_height,
    )


def write_empty_svg(output_path: Path, width: str, height: str, view_box: Optional[str]) -> None:
    ET.register_namespace("", SVG_NS)
    attrib = {
        "width": width,
        "height": height,
        "version": "1.1",
    }
    if view_box:
        attrib["viewBox"] = view_box
    root = ET.Element(f"{{{SVG_NS}}}svg", attrib=attrib)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(output_path, encoding="utf-8", xml_declaration=True)


def make_empty_head_svg(head_path: Path, output_path: Path) -> Path:
    head_root = parse_svg_root(head_path)
    width = head_root.get("width")
    height = head_root.get("height")
    view_box = head_root.get("viewBox")
    if not width or not height:
        if view_box:
            parts = view_box.replace(',', ' ').split()
            if len(parts) == 4:
                width = width or parts[2]
                height = height or parts[3]
    if not width or not height:
        raise ValueError(f"Head SVG {head_path} missing width/height and viewBox")
    write_empty_svg(output_path, width, height, view_box)
    return output_path


def empty_figabooth(order_id: str, output_path: Path) -> Figabooth:
    write_empty_svg(output_path, "57.2416", "81.6532", "0 0 57.2416 81.6532")
    return Figabooth(
        order_id=order_id,
        svg_path=output_path,
        width_px=57.2416,
        height_px=81.6532,
    )


def discover_orders(order_dir: Path) -> Sequence[OrderEntry]:
    pairs: List[OrderEntry] = []
    for head_path in order_dir.glob("*_head.svg"):
        order_id = head_path.stem.replace("_head", "")
        torso_path = order_dir / f"{order_id}_torso.svg"
        if not torso_path.exists():
            logging.warning("Skipping order %s: missing body SVG", order_id)
            continue
        torso_back_path = order_dir / f"{order_id}_torso_back.svg"
        pairs.append(
            OrderEntry(
                order_id=order_id,
                head_path=head_path,
                torso_path=torso_path,
                torso_back_path=torso_back_path if torso_back_path.exists() else None,
            )
        )
    pairs.sort(key=lambda item: item.order_id)
    return pairs


def svg_to_png_bytes(svg_path: Path, width_px: float, height_px: float) -> bytes:
    """Convert SVG to PNG bytes using cairosvg for better clipPath support."""
    with open(svg_path, 'rb') as svg_file:
        svg_data = svg_file.read()

    svg_text = decode_svg_bytes(svg_data, svg_path)
    project_font_dir = Path(__file__).resolve().with_name("fonts")
    local_font_map = get_project_font_map(str(project_font_dir))

    svg_text = convert_text_to_paths(svg_text, local_font_map)
    svg_data = svg_text.encode("utf-8")
    # Convert SVG to PNG with specified dimensions
    png_data = cairosvg.svg2png(
        bytestring=svg_data,
        output_width=int(width_px),
        output_height=int(height_px),
        unsafe=True,  # allow embedded data: fonts and external resources
    )
    return png_data


def layout_figabooths(figs: Sequence[Figabooth], pdf_path: Path) -> None:
    if not figs:
        logging.info("No figabooths generated; skipping PDF creation.")
        return

    _, page_height_pt = A4

    first_center_x_px = 51.4277
    first_center_y_px = 235.2401
    center_spacing_x_px = 45.2708
    center_spacing_y_px = 168.1983

    first_center_x_pt = first_center_x_px
    first_center_y_pt = page_height_pt - first_center_y_px
    center_spacing_x_pt = center_spacing_x_px
    center_spacing_y_pt = center_spacing_y_px

    fig_width_pt = figs[0].width_px
    fig_height_pt = figs[0].height_px

    max_cols = 12
    max_rows = 4
    per_page = max_cols * max_rows

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_buffer = io.BytesIO()
    c = canvas.Canvas(pdf_buffer, pagesize=A4)

    for index, fig in enumerate(figs):
        slot = index % per_page
        row = slot // max_cols
        col = slot % max_cols

        if slot == 0 and index != 0:
            c.showPage()

        center_x = first_center_x_pt + col * center_spacing_x_pt
        center_y = first_center_y_pt - row * center_spacing_y_pt
        x = center_x - (fig_width_pt / 2)
        y = center_y - (fig_height_pt / 2)

        # Use cairosvg for better clipPath support
        try:
            upscale_factor = 16
            png_data = svg_to_png_bytes(
                fig.svg_path,
                fig.width_px * upscale_factor,
                fig.height_px * upscale_factor,
            )
            img = Image.open(io.BytesIO(png_data))
            img_reader = ImageReader(img)
            c.drawImage(img_reader, x, y, width=fig_width_pt, height=fig_height_pt, mask='auto')
        except Exception as e:
            logging.warning("Failed to convert SVG %s with cairosvg: %s. Falling back to svglib.", fig.svg_path, e)
            # Fallback to original svglib method
            drawing = svg2rlg(str(fig.svg_path))
            if drawing.width == 0 or drawing.height == 0:
                logging.warning("Skipping empty figabooth SVG for order %s", fig.order_id)
                continue
            scale_x = fig_width_pt / drawing.width
            scale_y = fig_height_pt / drawing.height
            drawing.scale(scale_x, scale_y)
            renderPDF.draw(drawing, c, x, y)

    c.save()

    pdf_buffer.seek(0)

    if BACKGROUND_PDF.exists():
        background_reader = PdfReader(str(BACKGROUND_PDF))
        if len(background_reader.pages) == 0:
            logging.warning(
                "Background PDF %s has no pages; using blank background.", BACKGROUND_PDF
            )
        else:
            layout_reader = PdfReader(pdf_buffer)
            writer = PdfWriter()
            for index, layout_page in enumerate(layout_reader.pages):
                template_index = min(index, len(background_reader.pages) - 1)
                try:
                    # Access the page directly from the reader
                    background_page = background_reader.pages[template_index]
                    # Try the newer PyPDF2/PyPDF4 method first
                    if hasattr(background_page, 'merge_page'):
                        background_page.merge_page(layout_page)
                    elif hasattr(background_page, 'mergePage'):
                        background_page.mergePage(layout_page)
                    else:
                        # Fallback: create a copy and try merging
                        background_page = copy.deepcopy(background_page)
                        if hasattr(background_page, 'merge_page'):
                            background_page.merge_page(layout_page)
                        elif hasattr(background_page, 'mergePage'):
                            background_page.mergePage(layout_page)
                        else:
                            logging.error("Cannot find merge method on background page")
                            continue
                    writer.add_page(background_page)
                except Exception as e:
                    logging.error("Error merging pages for index %d: %s", index, e)
                    # Add just the layout page without background
                    writer.add_page(layout_page)
            with pdf_path.open("wb") as out_file:
                writer.write(out_file)
            return

    with pdf_path.open("wb") as out_file:
        out_file.write(pdf_buffer.getvalue())


def process_orders(order_dir: Path, fig_output_dir: Path, pdf_path: Path, pdf_back_path: Path) -> None:
    figabooths: List[Figabooth] = []
    figabooths_back: List[Figabooth] = []

    for entry in discover_orders(order_dir):
        fig_path = fig_output_dir / f"{entry.order_id}_figabooth.svg"
        figabooths.append(combine_order(entry.head_path, entry.torso_path, fig_path))

        if entry.torso_back_path:
            empty_head_path = fig_output_dir / f"{entry.order_id}_empty_head.svg"
            make_empty_head_svg(entry.head_path, empty_head_path)
            back_fig_path = fig_output_dir / f"{entry.order_id}_figabooth_back.svg"
            figabooths_back.append(
                combine_order(empty_head_path, entry.torso_back_path, back_fig_path)
            )
        else:
            back_placeholder_path = fig_output_dir / f"{entry.order_id}_figabooth_back_empty.svg"
            figabooths_back.append(empty_figabooth(entry.order_id, back_placeholder_path))

    layout_figabooths(figabooths, pdf_path)
    layout_figabooths(figabooths_back, pdf_back_path)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate A4 PDFs from paired head/body order SVGs.")
    parser.add_argument(
        "--order-dir",
        type=Path,
        default=Path("order"),
        help="Directory containing <order>_head.svg and <order>_body.svg files.",
    )
    parser.add_argument(
        "--fig-output-dir",
        type=Path,
        default=Path("output"),
        help="Directory where combined figabooth SVGs will be written.",
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        default=Path("figabooth.pdf"),
        help="Output PDF path.",
    )
    parser.add_argument(
        "--pdf-back",
        type=Path,
        default=Path("figabooth_back.pdf"),
        help="Output PDF path for torso_back.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level))

    process_orders(args.order_dir, args.fig_output_dir, args.pdf, args.pdf_back)


if __name__ == "__main__":
    main()
