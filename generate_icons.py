"""Generate PNG home-screen icons from the SVG design.
Run once: python generate_icons.py
"""
from PIL import Image, ImageDraw, ImageFilter


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def make_icon(size: int, maskable: bool = False) -> Image.Image:
    """Render the card-on-gradient design at the requested pixel size."""
    s = 1024  # render at high res then downsample for smooth edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Background: dark navy → black diagonal gradient with rounded corners
    bg_top    = (0x0a, 0x0d, 0x28)
    bg_bottom = (0x04, 0x04, 0x0f)
    bg = Image.new("RGB", (s, s), bg_bottom)
    bg_px = bg.load()
    for y in range(s):
        for x in range(s):
            t = (x + y) / (2 * s)
            bg_px[x, y] = lerp(bg_top, bg_bottom, t)
    # corner radius — full bleed for maskable (safe area is inner ~80%)
    radius = 0 if maskable else int(s * 112 / 512)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s, s), radius=radius, fill=255)
    img.paste(bg, (0, 0), mask)

    # ── Inner content scaled to leave maskable safe area
    scale = 0.78 if maskable else 1.0
    inset = int(s * (1 - scale) / 2)

    def sx(v):  # scale & offset coordinates from 512 design space
        return int(v * s / 512 * scale) + inset

    def sd(v):  # scale a distance
        return int(v * s / 512 * scale)

    # ── Card: cyan → magenta gradient, rounded
    card_x, card_y, card_w, card_h = 96, 156, 320, 200
    card_box = (sx(card_x), sx(card_y), sx(card_x + card_w), sx(card_y + card_h))
    c_top = (0x00, 0xe5, 0xff)
    c_bot = (0xc0, 0x60, 0xff)
    card_w_px = card_box[2] - card_box[0]
    card_h_px = card_box[3] - card_box[1]
    card_img = Image.new("RGB", (card_w_px, card_h_px), c_top)
    cp = card_img.load()
    for y in range(card_h_px):
        for x in range(card_w_px):
            t = (x + y) / (card_w_px + card_h_px)
            cp[x, y] = lerp(c_top, c_bot, t)
    card_mask = Image.new("L", (card_w_px, card_h_px), 0)
    ImageDraw.Draw(card_mask).rounded_rectangle(
        (0, 0, card_w_px, card_h_px), radius=sd(24), fill=255
    )
    img.paste(card_img, (card_box[0], card_box[1]), card_mask)

    # ── Magnetic stripe (dark band across card top)
    stripe_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd_draw = ImageDraw.Draw(stripe_layer)
    sd_draw.rectangle(
        (sx(96), sx(196), sx(96 + 320), sx(196 + 36)),
        fill=(4, 4, 15, int(255 * 0.55)),
    )
    img = Image.alpha_composite(img, stripe_layer)

    # ── Number block (small rounded rect)
    nb_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(nb_layer).rounded_rectangle(
        (sx(128), sx(296), sx(128 + 96), sx(296 + 24)),
        radius=sd(6),
        fill=(4, 4, 15, int(255 * 0.45)),
    )
    img = Image.alpha_composite(img, nb_layer)

    # ── Two chip circles (right side)
    chip_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    cd = ImageDraw.Draw(chip_layer)
    r = sd(22)
    cd.ellipse((sx(356) - r, sx(308) - r, sx(356) + r, sx(308) + r),
               fill=(4, 4, 15, int(255 * 0.35)))
    cd.ellipse((sx(388) - r, sx(308) - r, sx(388) + r, sx(308) + r),
               fill=(4, 4, 15, int(255 * 0.25)))
    img = Image.alpha_composite(img, chip_layer)

    # downsample to requested size with high quality
    if size != s:
        img = img.resize((size, size), Image.LANCZOS)
    return img


targets = [
    ("static/icon-180.png", 180, False),  # apple-touch-icon
    ("static/icon-192.png", 192, False),  # Android PWA
    ("static/icon-512.png", 512, False),  # Android PWA / splash
    ("static/icon-512-maskable.png", 512, True),
]

for path, size, maskable in targets:
    out = make_icon(size, maskable=maskable)
    out.save(path, "PNG", optimize=True)
    print(f"wrote {path} ({size}x{size})")
