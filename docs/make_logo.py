#!/usr/bin/env python3
"""Generate Yes, Dev logo (Option 3) as a genuine SVG: vector clouds from the
real _render_cloud lobe geometry, text outlined from the font so no font is
needed to render it."""
import random
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools.pens.svgPathPen import SVGPathPen

FONT="/System/Library/Fonts/SFNSRounded.ttf"
GREEN_LIT="#56C868"; GREEN_DARK="#1C7A30"; DEVGREEN="#56C868"
INK="#14161C"; WHITE="#F7FAFD"

# --- Option 3 params, in display px ---
FS=100; RADIUS=44; MARGIN=84; TY_FRAC=0.57
MARK_SCALE=0.94; MARK_DROP=0.12*FS; N_CLOUDS=18; SEED=31; UPPER_BIAS=0.50

# _render_cloud's silhouette: 5 lobes (cx,cy,r as fractions of w,h) + a base rect
LOBES=[(0.28,0.60,0.20),(0.50,0.52,0.26),(0.72,0.60,0.19),(0.40,0.43,0.17),(0.62,0.46,0.15)]
BASE=(0.24,0.58,0.78,0.84)  # x0,y0,x1,y1 fractions
CH_RATIO=0.78               # height = width*0.78

def glyphs(wght):
    f=TTFont(FONT); instantiateVariableFont(f,{"wght":wght},inplace=True)
    return f, f.getGlyphSet(), f.getBestCmap(), f['head'].unitsPerEm, f['hmtx']

BOLD=glyphs(760); REG=glyphs(400)

def text_paths(s, inst, x, baseline, fill):
    f,gs,cmap,upm,hmtx=inst; S=FS/upm; out=[]; adv_total=0
    for ch in s:
        gname=cmap[ord(ch)]; pen=SVGPathPen(gs); gs[gname].draw(pen)
        d=pen.getCommands()
        if d.strip():
            out.append(f'<path transform="translate({x+adv_total:.2f},{baseline:.2f}) scale({S:.5f},{-S:.5f})" d="{d}" fill="{fill}"/>')
        adv_total+=hmtx[gname][0]*S
    return "\n".join(out), adv_total

def text_width(s, inst):
    f,gs,cmap,upm,hmtx=inst; S=FS/upm
    return sum(hmtx[cmap[ord(ch)]][0]*S for ch in s)

def cloud_shapes(cw, flip=True):
    """The 5 lobes + base rect as SVG elements in a [0..cw]x[0..ch] box."""
    ch=cw*CH_RATIO; el=[]
    for (lx,ly,r) in LOBES:
        el.append(f'<ellipse cx="{lx*cw:.2f}" cy="{ly*ch:.2f}" rx="{r*cw:.2f}" ry="{r*cw:.2f}"/>')
    x0,y0,x1,y1=BASE
    el.append(f'<rect x="{x0*cw:.2f}" y="{y0*ch:.2f}" width="{(x1-x0)*cw:.2f}" height="{(y1-y0)*ch:.2f}"/>')
    inner="".join(el)
    tf=f'translate(0,{ch:.2f}) scale(1,-1)' if flip else ''
    return inner, cw, ch, tf

defs=[]; body=[]; fid=0
def blur_filter(cw, firm=True):
    global fid; fid+=1; std=cw*0.032
    fc='<feComponentTransfer><feFuncA type="table" tableValues="0 0 0.35 1 1"/></feComponentTransfer>' if firm else ''
    defs.append(f'<filter id="b{fid}" x="-30%" y="-30%" width="160%" height="160%">'
                f'<feGaussianBlur stdDeviation="{std:.2f}"/>{fc}</filter>')
    return f"b{fid}"

# gradients
defs.append(f'<linearGradient id="pale" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="#F7FAFF"/><stop offset="1" stop-color="#B0C1D6"/></linearGradient>')
defs.append(f'<linearGradient id="grn" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{GREEN_LIT}"/><stop offset="1" stop-color="{GREEN_DARK}"/></linearGradient>')

# ---- layout (mirrors build()) ----
w_yes=text_width("Yes,",BOLD); w_dev=text_width(" Dev",REG)
gap=FS*0.12; mark_w=FS*MARK_SCALE/CH_RATIO; mark_h=mark_w*CH_RATIO
content=w_yes+w_dev+gap+mark_w
W=int(content+MARGIN*2); H=int(FS*2.5); TY=H*TY_FRAC
x0=(W-content)/2

# ---- clouds behind (same seed/logic as raster scatter) ----
rng=random.Random(SEED)
tx0,ty0,tx1,ty1=x0,TY-FS*0.5,x0+content,TY+FS*0.5
for i in range(N_CLOUDS):
    cw=rng.randint(26,58)
    cy=rng.randint(int(H*0.04),int(H*0.5)) if rng.random()<UPPER_BIAS else rng.randint(int(H*0.44),int(H*0.92))
    cx=rng.randint(int(W*0.02),int(W*0.95))
    over=(tx0-cw<cx<tx1) and (ty0-cw<cy<ty1)
    a=rng.uniform(0.15,0.32) if over else rng.uniform(0.40,0.85)
    rng.randint(0,99999)
    inner,cwv,chv,tf=cloud_shapes(cw)
    fb=blur_filter(cw)
    body.append(f'<g opacity="{a:.2f}" transform="translate({cx},{cy})"><g filter="url(#{fb})" transform="{tf}" fill="url(#pale)">{inner}</g></g>')

# baseline so caps sit centered on TY (tuned)
baseline=TY+FS*0.35
# ---- text ----
t1,adv1=text_paths("Yes,",BOLD,x0,baseline,WHITE); body.append(t1)
t2,adv2=text_paths(" Dev",REG,x0+w_yes,baseline,DEVGREEN); body.append(t2)

# ---- green cloud mark + check, lowered ----
mx=x0+w_yes+w_dev+gap; my=TY-mark_h/2+MARK_DROP
inner,cwv,chv,tf=cloud_shapes(mark_w)
mfb=blur_filter(mark_w, firm=True)
body.append(f'<g transform="translate({mx:.2f},{my:.2f})">'
            f'<g filter="url(#{mfb})" transform="{tf}" fill="url(#grn)">{inner}</g>')
# check: scaled to the mark, matching the raster geometry
s=mark_h*0.0118*1.34; ccx=mark_w/2; ccy=mark_h/2-0.02*mark_h
pts=f'{ccx-13*s:.1f},{ccy+0.5*s:.1f} {ccx-4.5*s:.1f},{ccy+9.5*s:.1f} {ccx+13.5*s:.1f},{ccy-10*s:.1f}'
body.append(f'<polyline points="{pts}" fill="none" stroke="#fff" stroke-width="{5.6*s:.1f}" stroke-linecap="round" stroke-linejoin="round"/></g>')

svg=(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Yes, Dev">'
     f'<defs>{"".join(defs)}<clipPath id="rc"><rect width="{W}" height="{H}" rx="{RADIUS}" ry="{RADIUS}"/></clipPath></defs>'
     f'<g clip-path="url(#rc)"><rect width="{W}" height="{H}" fill="{INK}"/>{"".join(body)}</g></svg>')
open("/Users/thickdiggy/claude_workspace/yes-dev/docs/logo.svg","w").write(svg)
open(f"{__import__('os').path.dirname(__file__)}/logo.svg","w").write(svg)
print(f"SVG {W}x{H}, {len(svg)} bytes, clouds={N_CLOUDS}, filters={fid}")
