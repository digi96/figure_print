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
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple
from xml.etree import ElementTree as ET

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPDF
except ImportError as exc:  # pragma: no cover - runtime guard
    raise SystemExit(
        "Missing dependencies. Please install them with 'pip install reportlab svglib'."
    ) from exc

SVG_NS = "http://www.w3.org/2000/svg"
PX_TO_MM = 0.2645833333


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


@dataclass
class Figabooth:
    order_id: str
    svg_path: Path
    width_px: float
    height_px: float


def combine_order(head_path: Path, body_path: Path, output_path: Path) -> Figabooth:
    head_tree = ET.parse(head_path)
    body_tree = ET.parse(body_path)
    head_root = head_tree.getroot()
    body_root = body_tree.getroot()

    (head_width, head_height, body_width, body_height, total_width, total_height) = (
        figabooth_dimensions(head_root, body_root)
    )

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

    head_group = clone_as_group(head_root)
    head_offset_x = (total_width - head_width) / 2
    head_group.set("transform", f"translate({head_offset_x},0)")
    root.append(head_group)

    body_group = clone_as_group(body_root)
    body_offset_x = (total_width - body_width) / 2
    body_group.set("transform", f"translate({body_offset_x},{head_height})")
    root.append(body_group)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(output_path, encoding="utf-8", xml_declaration=True)

    return Figabooth(
        order_id=output_path.stem.replace("_figabooth", ""),
        svg_path=output_path,
        width_px=total_width,
        height_px=total_height,
    )


def discover_orders(order_dir: Path) -> Sequence[Tuple[Path, Path, str]]:
    pairs: List[Tuple[Path, Path, str]] = []
    for head_path in order_dir.glob("*_head.svg"):
        order_id = head_path.stem[:-5]
        body_path = order_dir / f"{order_id}_body.svg"
        if not body_path.exists():
            logging.warning("Skipping order %s: missing body SVG", order_id)
            continue
        pairs.append((head_path, body_path, order_id))
    pairs.sort(key=lambda item: item[2])
    return pairs


def layout_figabooths(figs: Sequence[Figabooth], pdf_path: Path) -> None:
    if not figs:
        logging.info("No figabooths generated; skipping PDF creation.")
        return

    page_width_pt, page_height_pt = A4
    # Maintain a vertical gap of 78.383 px between rows (converted to points).
    v_spacing_pt = 78.383 * PX_TO_MM * mm

    start_x_px = 22.793
    top_margin_px = 31.625
    overlap_px = 11.929

    start_x_pt = start_x_px * PX_TO_MM * mm
    top_margin_pt = top_margin_px * PX_TO_MM * mm
    overlap_pt = overlap_px * PX_TO_MM * mm

    fig_width_pt = figs[0].width_px * PX_TO_MM * mm
    fig_height_pt = figs[0].height_px * PX_TO_MM * mm

    start_y = page_height_pt - top_margin_pt - fig_height_pt
    if start_y < 0:
        start_y = 0

    step_x_pt = fig_width_pt - overlap_pt
    if step_x_pt <= 0:
        step_x_pt = fig_width_pt

    available_width_pt = page_width_pt - start_x_pt
    if available_width_pt <= fig_width_pt:
        max_cols = 1
    else:
        max_cols = max(1, math.floor((available_width_pt - fig_width_pt) / step_x_pt) + 1)

    row_height_pt = fig_height_pt + v_spacing_pt
    max_rows = 0
    current_y = start_y
    while current_y >= 0:
        max_rows += 1
        current_y -= row_height_pt
    if max_rows == 0:
        max_rows = 1

    per_page = max_cols * max_rows

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(pdf_path), pagesize=A4)

    for index, fig in enumerate(figs):
        slot = index % per_page
        row = slot // max_cols
        col = slot % max_cols

        if slot == 0 and index != 0:
            c.showPage()

        x = start_x_pt + col * step_x_pt
        y = start_y - row * (fig_height_pt + v_spacing_pt)

        drawing = svg2rlg(str(fig.svg_path))
        if drawing.width == 0 or drawing.height == 0:
            logging.warning("Skipping empty figabooth SVG for order %s", fig.order_id)
            continue
        scale_x = fig_width_pt / drawing.width
        scale_y = fig_height_pt / drawing.height
        drawing.scale(scale_x, scale_y)
        renderPDF.draw(drawing, c, x, y)

    c.save()


def process_orders(order_dir: Path, fig_output_dir: Path, pdf_path: Path) -> None:
    figabooths: List[Figabooth] = []
    for head_path, body_path, order_id in discover_orders(order_dir):
        fig_path = fig_output_dir / f"{order_id}_figabooth.svg"
        figabooths.append(combine_order(head_path, body_path, fig_path))

    layout_figabooths(figabooths, pdf_path)


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

    process_orders(args.order_dir, args.fig_output_dir, args.pdf)


if __name__ == "__main__":
    main()
