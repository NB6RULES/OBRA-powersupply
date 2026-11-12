"""Draws the PSU Studio icon: amber tile, dark power ring with a lightning bolt."""
# Usage: python tools/make_icon.py app/assets   (needs Pillow)
import math, sys
from PIL import Image, ImageDraw, ImageFilter
N = 1024
out = sys.argv[1]

def tile():
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    # vertical amber gradient
    grad = Image.new("RGBA", (N, N))
    gd = ImageDraw.Draw(grad)
    top, bot = (255, 200, 90), (255, 150, 16)
    for y in range(N):
        f = y / (N - 1)
        gd.line([(0, y), (N, y)], fill=tuple(int(top[k] + (bot[k] - top[k]) * f) for k in range(3)) + (255,))
    m = Image.new("L", (N, N), 0)
    pad = int(N * 0.04)
    ImageDraw.Draw(m).rounded_rectangle([pad, pad, N - pad, N - pad], radius=int(N * 0.22), fill=255)
    img.paste(grad, (0, 0), m)
    return img, m

img, mask = tile()
d = ImageDraw.Draw(img)
ink = (22, 16, 8, 255)
cx, cy = N / 2, N * 0.53
R, W = N * 0.30, N * 0.085
# power ring with a gap at the top (gap ±38°)
gap = 40
d.arc([cx - R, cy - R, cx + R, cy + R], start=-90 + gap, end=270 - gap, fill=ink, width=int(W))
# round caps on the ring ends
for ang in (-90 + gap, 270 - gap):
    a = math.radians(ang)
    ex, ey = cx + (R - W / 2) * math.cos(a), cy + (R - W / 2) * math.sin(a)
    d.ellipse([ex - W / 2, ey - W / 2, ex + W / 2, ey + W / 2], fill=ink)
# lightning bolt where the power-symbol stem would be
s = N / 1024
bolt = [(560, 120), (380, 560), (505, 560), (455, 860), (660, 420), (530, 420), (600, 120)]
d.polygon([(x * s, y * s) for x, y in bolt], fill=ink)
# thin amber gap around the bolt so it reads separately from the ring
halo = Image.new("L", (N, N), 0)
ImageDraw.Draw(halo).polygon([(x * s, y * s) for x, y in bolt], fill=255)
halo = halo.filter(ImageFilter.MaxFilter(41))
ring = Image.new("L", (N, N), 0)
ImageDraw.Draw(ring).arc([cx - R, cy - R, cx + R, cy + R], start=-90 + gap, end=270 - gap, fill=255, width=int(W))
cut = Image.new("L", (N, N), 0)
cut.paste(ring, (0, 0), halo)
base, _ = tile()
img.paste(base, (0, 0), cut)
d = ImageDraw.Draw(img)
d.polygon([(x * s, y * s) for x, y in bolt], fill=ink)
img.putalpha(Image.composite(img.getchannel("A"), Image.new("L", (N, N), 0), mask))

img.save(out + "/icon-1024.png")
img.resize((256, 256), Image.LANCZOS).save(out + "/icon-256.png")
img.resize((64, 64), Image.LANCZOS).save(out + "/favicon.png")
sizes = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
img.save(out + "/psu_studio.ico", sizes=sizes)
