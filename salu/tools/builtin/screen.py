"""Screenshot tool. With analyze=true the image is sent to Claude so it can read the screen."""

from __future__ import annotations

import asyncio
import base64
import io
from datetime import datetime

from ...security.permissions import Risk
from ...winos import IS_WINDOWS
from ..base import ToolResult, salu_tool

MODEL_MAX_SIDE = 1568      # Claude's recommended max image dimension
THUMB_WIDTH = 520
KEEP_SCREENSHOTS = 50


def _encode(img, max_side: int, quality: int) -> bytes:
    from PIL import Image
    im = img.convert("RGB")
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _capture(all_screens: bool, folder, save: bool, analyze: bool):
    from PIL import ImageGrab
    img = ImageGrab.grab(all_screens=all_screens)
    out = {"size": img.size, "path": None, "model": None, "thumb": None}
    if save:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"screen-{datetime.now():%Y%m%d-%H%M%S}.png"
        img.save(path, "PNG", optimize=True)
        out["path"] = str(path)
        old = sorted(folder.glob("screen-*.png"))[:-KEEP_SCREENSHOTS]
        for f in old:
            f.unlink(missing_ok=True)
    if analyze:
        out["model"] = base64.b64encode(_encode(img, MODEL_MAX_SIDE, 80)).decode()
    out["thumb"] = "data:image/jpeg;base64," + base64.b64encode(_encode(img, THUMB_WIDTH, 70)).decode()
    return out


def _risk(args) -> Risk:
    # Sending the screen to Claude means its content leaves this computer.
    return Risk.MEDIUM if args.get("analyze", True) else Risk.LOW


def _describe(args) -> list[str]:
    lines = ["Capture " + ("all monitors" if args.get("all_screens") else "the main monitor")]
    if args.get("analyze", True):
        lines.append("Send the image to Claude (Anthropic) to read the screen")
    if args.get("save", True):
        lines.append("Save PNG to data/screenshots/")
    return lines


@salu_tool("take_screenshot",
           "Capture the screen. analyze=true (default) lets you see the image to read/describe what is on screen "
           "(needs user confirmation because screen contents are sent to Claude). save=true stores a PNG locally.",
           risk="low", category="screen", risk_fn=_risk, describe_fn=_describe,
           properties={"all_screens": {"type": "boolean", "default": False},
                       "analyze": {"type": "boolean", "default": True},
                       "save": {"type": "boolean", "default": True}})
async def take_screenshot(args, ctx):
    if not IS_WINDOWS:
        return ToolResult.failure("Screenshots are only supported on Windows.")
    analyze = args.get("analyze", True)
    try:
        r = await asyncio.to_thread(_capture, bool(args.get("all_screens")), ctx.settings.path("data/screenshots"),
                                    args.get("save", True), analyze)
    except Exception as e:
        return ToolResult.failure(f"Screenshot failed: {type(e).__name__}: {e}")
    w, h = r["size"]
    text = f"Screenshot captured ({w}x{h})." + (f" Saved: {r['path']}" if r["path"] else "")
    # data is empty when analyze=false: the UI still gets the thumbnail, Claude gets no image.
    image = {"data": r["model"] or "", "mime": "image/jpeg", "thumb": r["thumb"]}
    return ToolResult.success(text, {"path": r["path"], "width": w, "height": h}, images=[image])


TOOLS = [take_screenshot]
