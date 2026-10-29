"""Pixel-only capabilities for generated programs. No simulator or judge imports."""

from __future__ import annotations

import cv2
import numpy as np

DIGITS = ["111101101101111", "010110010010111", "111001111100111", "111001111001111",
          "101101111001001", "111100111001111", "111100111101111", "111001010010010",
          "111101111101111", "111101111001111"]


def image_size(image):
    return [int(image.shape[1]), int(image.shape[0])]


def pixel(image, x, y):
    if type(x) is not int or type(y) is not int or not 0 <= x < image.shape[1] or not 0 <= y < image.shape[0]:
        raise ValueError("pixel outside image")
    return image[y, x].tolist()


def mask_color(image, color, tolerance=0):
    if len(color) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in color):
        raise ValueError("color requires three channel values")
    if type(tolerance) is not int or not 0 <= tolerance <= 100:
        raise ValueError("invalid color tolerance")
    color = np.array(color, dtype=np.int16)
    return cv2.inRange(image, np.maximum(0, color - tolerance).astype(np.uint8),
                       np.minimum(255, color + tolerance).astype(np.uint8))


def count_color(image, x0, y0, x1, y1, color, tolerance=0):
    if any(type(v) is not int for v in (x0, y0, x1, y1)):
        raise ValueError("rectangle coordinates must be integers")
    if not 0 <= x0 <= x1 <= image.shape[1] or not 0 <= y0 <= y1 <= image.shape[0]:
        raise ValueError("rectangle outside image")
    if x0 == x1 or y0 == y1:
        return 0
    return int(cv2.countNonZero(mask_color(image[y0:y1, x0:x1], color, tolerance)))


def components(image, color, tolerance=0):
    number, labels, stats, centers = cv2.connectedComponentsWithStats(mask_color(image, color, tolerance))
    if number > 1025:
        raise ValueError("too many color components")
    return [{"x": float(centers[i][0]), "y": float(centers[i][1]), "area": int(stats[i, 4]),
             "left": int(stats[i, 0]), "top": int(stats[i, 1]),
             "width": int(stats[i, 2]), "height": int(stats[i, 3])} for i in range(1, number)]


def read_digits(image, x, y, count=4, scale=2, stride=8):
    """Exact 3x5 glyph reading from pixels; unknown glyphs remain unknown."""
    if not all(type(v) is int for v in (x, y, count, scale, stride)):
        raise ValueError("glyph geometry must be integer")
    if not 1 <= count <= 8 or not 1 <= scale <= 4 or not 3 <= stride <= 32:
        raise ValueError("invalid glyph geometry")
    value = 0
    for i in range(count):
        bits = ""
        for row in range(5):
            for col in range(3):
                rgb = pixel(image, x + i * stride + col * scale, y + row * scale)
                bits += "1" if min(rgb) >= 240 else "0"
        if bits not in DIGITS:
            return None
        value = 10 * value + DIGITS.index(bits)
    return value


HELPERS = {"image_size": image_size, "pixel": pixel, "count_color": count_color,
           "components": components, "read_digits": read_digits}
HELP_TEXT = """All helpers use only the supplied RGB image, and return plain data.
image_size(image) -> [width,height]. pixel(image,x,y) -> [R,G,B].
count_color(image,x0,y0,x1,y1,[R,G,B],tolerance=0) -> number of matching pixels in half-open rectangle.
components(image,[R,G,B],tolerance=0) -> list of {x,y,area,left,top,width,height} connected regions.
read_digits(image,x,y,count=4,scale=2,stride=8) -> integer or None for unknown 3x5 glyphs.
No helper has access to game state, future spawns, test seeds, evaluator, files, or network."""
