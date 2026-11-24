#!/usr/bin/env python3
"""Codsworth pixel glyph: shared geometry for GIF, PNG, SVG and terminal output.

The model and pre-war reference pictures inform the silhouette, warm chrome,
articulated tool arms and compact thruster. Coordinates use a 96 x 112 pixel grid.
"""
import argparse
import json
import math
from pathlib import Path

BG, OUTLINE, DARK, SHADOW, MID, SHELL, LIGHT, HI = range(8)
GLASS, LENS, GLINT, FLAME, CORE, RUST, EMBER, SEAM = range(8, 16)
PALETTE = {
    BG: (18, 20, 24), OUTLINE: (30, 32, 31), DARK: (48, 50, 45),
    SHADOW: (79, 83, 74), MID: (119, 123, 108), SHELL: (162, 165, 145),
    LIGHT: (202, 204, 182), HI: (235, 232, 206), GLASS: (16, 24, 27),
    LENS: (43, 61, 65), GLINT: (218, 231, 227), FLAME: (245, 149, 57),
    CORE: (255, 228, 147), RUST: (114, 78, 49), EMBER: (195, 76, 30),
    SEAM: (57, 63, 57),
}
W, H, CX, CY, R = 96, 112, 48, 35, 21
FRAME_MS = 80
ACTIVITIES = ('idle', 'talking', 'listening', 'thinking')
EXPRESSIONS = ('neutral', 'pleased', 'skeptical', 'concerned', 'annoyed')


class Grid:
    def __init__(self):
        self.g = [[BG] * W for _ in range(H)]

    def put(self, r, c, material):
        r, c = round(r), round(c)
        if 0 <= r < H and 0 <= c < W:
            self.g[r][c] = material

    def rect(self, r0, r1, c0, c1, material):
        for r in range(round(r0), round(r1) + 1):
            for c in range(round(c0), round(c1) + 1):
                self.put(r, c, material)

    def ellipse(self, r, c, ry, rx, material):
        for y in range(math.floor(r - ry), math.ceil(r + ry) + 1):
            for x in range(math.floor(c - rx), math.ceil(c + rx) + 1):
                if ((y - r) / ry) ** 2 + ((x - c) / rx) ** 2 <= 1:
                    self.put(y, x, material)

    def disc(self, r, c, radius, material):
        self.ellipse(r, c, radius, radius, material)

    def seg(self, a, b, width, material):
        dy, dx = b[0] - a[0], b[1] - a[1]
        length = dy * dy + dx * dx or 1
        margin = width / 2
        for r in range(math.floor(min(a[0], b[0]) - margin), math.ceil(max(a[0], b[0]) + margin) + 1):
            for c in range(math.floor(min(a[1], b[1]) - margin), math.ceil(max(a[1], b[1]) + margin) + 1):
                t = max(0, min(1, ((r - a[0]) * dy + (c - a[1]) * dx) / length))
                if (r - a[0] - t * dy) ** 2 + (c - a[1] - t * dx) ** 2 <= margin ** 2:
                    self.put(r, c, material)

    def joint(self, point, radius=3):
        r, c = point
        self.disc(r, c, radius + 1, OUTLINE)
        self.disc(r, c, radius, MID)
        self.disc(r, c, radius - 1, DARK)
        self.put(r - 1, c - 1, LIGHT)
        self.put(r, c, SHELL)

    def strut(self, a, b, width=4):
        self.seg(a, b, width + 2, OUTLINE)
        self.seg(a, b, width, SHADOW)
        self.seg((a[0], a[1] - 1), (b[0], b[1] - 1), 1, LIGHT)

    def ball(self, cr, cc):
        for r in range(math.floor(cr - R), math.ceil(cr + R) + 1):
            for c in range(math.floor(cc - R), math.ceil(cc + R) + 1):
                x, y = (c - cc) / R, (r - cr) / R
                d = x * x + y * y
                if d > 1:
                    continue
                if d > .91:
                    material = MID if x + y < 0 else OUTLINE
                elif y > .25:
                    material = DARK if d < .77 else SHADOW
                elif y > -.08:
                    material = SHADOW if x > .12 else MID
                else:
                    material = SHELL if x < .45 else MID
                # Broad reflected highlights follow the round shell, not a diagonal split.
                if d < .87 and ((x + .48) / .18) ** 2 + ((y + .1) / .48) ** 2 < 1:
                    material = HI if y < .06 else LIGHT
                if d < .87 and ((x - .1) / .14) ** 2 + ((y + .04) / .3) ** 2 < 1:
                    material = LIGHT
                if d < .87 and y < -.58:
                    material = LIGHT if x < .25 else SHELL
                self.put(r, c, material)
        # Dome joint, cap, three lower vents and warm lower rim.
        for x in range(-17, 18):
            y = -12 + round(2 * (x / 17) ** 2)
            self.put(cr + y, cc + x, SEAM)
            self.put(cr + y - 1, cc + x, LIGHT)
        self.rect(cr - 19, cr - 18, cc - 5, cc + 5, MID)
        self.rect(cr - 20, cr - 20, cc - 3, cc + 4, LIGHT)
        for y, width in ((11, 10), (14, 8), (17, 5)):
            self.rect(cr + y, cr + y, cc - width // 2 + 3, cc + width // 2 + 3, OUTLINE)
            self.rect(cr + y + 1, cr + y + 1, cc - width // 2 + 3, cc + width // 2 + 2, MID)
        self.rect(cr + 19, cr + 19, cc - 5, cc + 4, RUST)

    def optic(self, r, c, radius, look=0, aperture=.4, subdued=False, shutter=0):
        """Recessed camera iris: fixed barrel, radial blades, centered opening."""
        r, c = round(r), round(c)
        subdued = subdued >= .5
        self.ellipse(r + 2, c + 1, radius + 1, radius + 1, OUTLINE)
        self.disc(r, c, radius, MID)
        self.disc(r - 1, c - 1, radius - 1, LIGHT)
        self.disc(r, c, radius - 2, DARK)
        self.disc(r, c, radius - 3, MID)
        inner = radius - 3
        stop = 1.05 + max(0, min(1, aperture)) * 3.45
        # Six overlapping sectors leave a centered hexagonal opening. The blades
        # turn as the aperture contracts; neither a pupil nor an eyelid translates.
        angle = .25 * (1 - aperture)
        normals = [(math.cos(angle + i * math.tau / 6),
                    math.sin(angle + i * math.tau / 6)) for i in range(6)]
        for dy in range(-inner, inner + 1):
            for dx in range(-inner, inner + 1):
                distance = math.hypot(dx, dy)
                if distance > inner - .25:
                    continue
                opening = max(dx * nx + dy * ny for nx, ny in normals) <= stop
                if opening:
                    material = GLASS
                else:
                    theta = (math.atan2(dy, dx) - angle - .14 * distance) % math.tau
                    sector = theta / (math.tau / 6)
                    material = (DARK if int(sector) % 2 else SHADOW) if subdued else (SHADOW if int(sector) % 2 else MID)
                    if sector % 1 < .12:
                        material = MID if subdued else SHELL
                self.put(r + dy, c + dx, material)
        if shutter:
            # Stylized mechanical upper shutter, entirely inside the fixed bezel.
            # Opposite slopes give a traditional glare without deforming the barrel.
            for dy in range(-inner, inner + 1):
                for dx in range(-inner, inner + 1):
                    boundary = -6 + abs(shutter) * 5 + shutter * dx * .45
                    if dx * dx + dy * dy < (inner - .25) ** 2 and dy < boundary:
                        self.put(r + dy, c + dx, SHADOW if dy < boundary - 1 else MID)
        # Reflections belong to the protective glass; they do not track the iris.
        self.put(r - 3, c - 3, GLINT)
        self.put(r + 3, c + 3, LENS)
        for dy, dx in ((-7, 0), (0, -7), (7, 0), (0, 7)):
            self.put(r + dy, c + dx, SHADOW)
        self.put(r - radius, c - 1, HI)


def frame(bob=0, arms=(0, 0, 0), lp=0, rp=0, flame=0, turn=0,
          apertures=(.4, .4), gaze=0, voice=0, reach=0, lift=0, grip=0, tilt=0, glare=False):
    g = Grid()
    cr = CY + bob
    la, ra, rear = arms

    def p(y, x):
        return cr + y, CX + x

    # Rear tool arm and rear optic are behind the shell and forward arms.
    rear_sh, rear_elbow, nozzle = p(23, -9), p(28 + rear, -26), p(19 + rear, -35)
    g.strut(rear_sh, rear_elbow, 3)
    g.strut(rear_elbow, nozzle, 2)
    g.joint(rear_elbow, 2)
    g.seg(nozzle, p(19 + rear, -41), 4, DARK)
    g.seg(p(18 + rear, -36), p(18 + rear, -42), 1, LIGHT)
    g.rect(cr + 18 + rear, cr + 21 + rear, CX - 43, CX - 41, MID)
    g.rect(cr + 19 + rear, cr + 20 + rear, CX - 45, CX - 44, OUTLINE)
    g.seg(p(6, -15), p(8, -27), 6, DARK)
    g.seg(p(5, -15), p(6, -28), 2, SHELL)
    g.ellipse(cr + 8, CX - 29, 4, 3, LIGHT)
    g.ellipse(cr + 8, CX - 30, 3, 2, GLASS)
    g.put(cr + 7, CX - 30, GLINT)

    # Compact gimbal, ribbed thruster and visible hover flame.
    g.ellipse(cr + 23, CX, 4, 12, OUTLINE)
    g.ellipse(cr + 22, CX, 3, 12, MID)
    g.rect(cr + 23, cr + 32, CX - 6, CX + 6, DARK)
    g.rect(cr + 23, cr + 32, CX - 5, CX - 3, MID)
    for y in (25, 28, 31):
        g.rect(cr + y, cr + y, CX - 5, CX + 5, SHELL)
    g.ellipse(cr + 34, CX, 3, 8, OUTLINE)
    g.rect(cr + 33, cr + 36, CX - 7, CX + 7, DARK)
    g.rect(cr + 33, cr + 35, CX - 6, CX - 4, SHADOW)
    length = 7 + round(flame)
    for y in range(length):
        width = max(0, round(3 * (1 - y / length)))
        g.rect(cr + 38 + y, cr + 38 + y, CX - width, CX + width, EMBER)
        if width:
            g.rect(cr + 38 + y, cr + 38 + y, CX - width + 1, CX + width - 1, FLAME)
        if y < length // 2:
            g.put(cr + 38 + y, CX, CORE)

    # Anchored shoulders, bent links and distinct left saw/right gripper.
    left = [p(25, -16), p(42 + la, -22), p(54 + la, -10),
            p(63 + la, -15), p(63 + la, -31), p(52 + la, -31)]
    right = [p(25, 16), p(43 + ra - lift * .35, 14 + reach * .25),
             p(59 + ra - lift * .65, 19 + reach * .25),
             p(57 + ra - lift * .85, 31 + reach * .35),
             p(42 + ra - lift, 33 + reach)]
    for points in (left, right):
        for index, (a, b) in enumerate(zip(points, points[1:])):
            g.strut(a, b, 5 if index == 0 else 2)
        for index, point in enumerate(points):
            g.joint(point, 3 if index < 2 else 2)
        # Ribbing on the substantial upper actuator housing.
        a, b = points[:2]
        for t in (.3, .5, .7):
            y, x = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
            g.seg((y, x - 2), (y, x + 2), 1, MID)

    sr, sc = cr + 50 + la, CX - 36
    for tooth in range(12):
        a = turn + tooth * math.tau / 12
        g.seg((sr + 6 * math.sin(a), sc + 6 * math.cos(a)),
              (sr + 8 * math.sin(a), sc + 8 * math.cos(a)), 2, SHELL)
    g.disc(sr, sc, 6, MID)
    g.disc(sr, sc, 4, DARK)
    g.seg((sr + 3, sc - 5), (sr - 1, sc + 5), 2, SHADOW)
    g.disc(sr, sc, 2, SHELL)
    g.put(sr - 1, sc - 1, HI)

    wr, wc = right[-1]
    opening = 5 - 3 * grip
    for side in (-1, 1):
        a, b, c = (wr - 1, wc), (wr - 5, wc + side * opening), (wr - 11, wc + side * (opening - 1))
        g.strut(a, b, 2)
        g.strut(b, c, 2)
        g.seg(c, (c[0] + 1, c[1] - side * 2), 2, SHELL)

    g.ball(cr, CX)
    voice = round(voice)
    if voice:
        g.rect(cr + 14, cr + 14, CX + 1, CX + 2 + voice, FLAME)

    # Heavy articulated eyestalks sit beside the dome, as in the reference.
    for side, drift in ((-1, lp), (1, rp)):
        root = p(-3, side * 17)
        hinge = p(-2, side * 24)
        eye = p(-17 + drift + (2 if side == 1 else 0), side * 26 + gaze)
        g.strut(root, hinge, 5)
        g.strut(hinge, (eye[0] + 4, eye[1]), 5)
        g.joint(root, 2)
        g.optic(*eye, 9, aperture=apertures[0 if side == -1 else 1],
                subdued=glare, shutter=-side * glare)
    if tilt:
        # Rotate the complete assembly together so joints stay attached.
        angle = math.radians(tilt)
        c, s = math.cos(angle), math.sin(angle)
        pivot_y = cr + 25
        rotated = [[BG] * W for _ in range(H)]
        for y in range(H):
            for x in range(W):
                sx = round(CX + (x - CX) * c + (y - pivot_y) * s)
                sy = round(pivot_y - (x - CX) * s + (y - pivot_y) * c)
                if 0 <= sx < W and 0 <= sy < H:
                    rotated[y][x] = g.g[sy][sx]
        return rotated
    return g.g


def poses(n=32, state='idle', expression='neutral'):
    """Combine an activity and an expression, preserving the same canvas/loop phase."""
    if state not in ACTIVITIES or expression not in EXPRESSIONS:
        raise ValueError('Unknown activity or expression')
    if n < 1:
        raise ValueError('Frame count must be positive')
    # Eye angles, independent radial apertures, stalk direction and arm posture.
    poses = {
        'neutral': (0, 0, (.4, .4), 0, (0, 0)),
        'pleased': (-2, -2, (.92, .92), 0, (-1, -1)),
        'skeptical': (-2, 2, (.95, .02), 1, (0, 2)),
        'concerned': (1, -1, (.95, .8), -1, (-1, -2)),
        'annoyed': (2, 2, (.75, .75), 0, (-2, 2)),
    }
    left, right, apertures, gaze, posture = poses[expression]
    gestures = {
        'neutral': (0, 0, 0, 0, 2),
        'pleased': (2, 8, 0, -2, 3),
        'skeptical': (-4, 12, .35, 3, 1),
        'concerned': (-10, 9, .7, -2, 1),
        'annoyed': (-6, 2, .85, 0, .4),
    }
    reach, lift, grip, tilt, hover = gestures[expression]
    result = []
    for index in range(n):
        phase = math.tau * index / n
        speech = max(0, round(2 * math.sin(phase * 3))) if state == 'talking' else 0
        lp = left + (0 if expression == 'annoyed' else round(math.sin(phase + .4)))
        rp = right + (0 if expression == 'annoyed' else round(math.sin(phase + 2)))
        if state == 'listening':
            lp -= 1
            rp -= 1
        elif state == 'thinking':
            lp += 1
            rp -= 2
        result.append(dict(
            bob=round(hover * math.sin(phase)),
            arms=(posture[0] + round(min(1.5, hover) * math.sin(phase - .8)) - speech,
                  posture[1] + round(min(1.5, hover) * math.sin(phase + 1.5)),
                  round(math.sin(phase + 2.8))),
            lp=lp, rp=rp, apertures=apertures, gaze=round(2 * math.sin(phase)) if state == 'thinking' else gaze,
            reach=-12 if state == 'thinking' else reach,
            lift=19 + round(math.sin(phase)) if state == 'thinking' else lift,
            grip=.65 if state == 'thinking' else grip,
            tilt=3 if state == 'thinking' else tilt, glare=expression == 'annoyed',
            voice=speech, flame=round(2 * math.sin(phase * 3)), turn=phase / 3,
        ))
    return result


def frames(n=32, state='idle', expression='neutral'):
    return [frame(**pose) for pose in poses(n, state, expression)]


ACTIONS = ('open-panel', 'present-result', 'inspect-error')


def action_frames(name, n=48):
    """One-shot gestures with a grip anchor and explicit contact cues per frame."""
    if name not in ACTIONS or n < 2:
        raise ValueError('Unknown action or insufficient frames')

    def ease(value):
        value = max(0, min(1, value))
        return value * value * (3 - 2 * value)

    animation, cues = [], []
    for index in range(n):
        t = index / (n - 1)
        prepare = ease(t / .3)
        settle = 1 - ease((t - .78) / .22)
        hold = prepare * settle
        reach, lift = 3 * hold, 12 * hold
        grip = ease((t - .25) / .12) * (1 - ease((t - .78) / .12))
        lp, rp = round(-hold), round(-2 * hold)
        apertures = (.65, .65)
        if name == 'present-result':
            lift = 16 * hold
            apertures = (.9, .9)
            lp = rp = round(-2 * hold)
        elif name == 'inspect-error':
            apertures = (.85, .65 - .5 * hold)
            lp, rp = round(2 * hold), round(-3 * hold)
        bob = round(-2 * hold)
        animation.append(frame(bob=bob, reach=reach, lift=lift, grip=grip,
                               lp=lp, rp=rp, apertures=apertures,
                               gaze=round(hold), flame=round(math.sin(t * math.tau * 2))))
        cues.append({'progress': t, 'contact': .37 <= t < .78,
                     'phase': 'reach' if t < .3 else 'contact' if t < .37 else 'move' if t < .78 else 'settle',
                     'grip': [CX + 33 + reach, CY + bob + 31 - lift]})
    return animation, cues


def export_catalog(directory):
    """Native-resolution horizontal sprite sheets for each activity/expression pair."""
    from PIL import Image
    directory.mkdir(parents=True, exist_ok=True)
    catalog = {'width': W, 'height': H, 'frameMs': FRAME_MS,
               'activities': list(ACTIVITIES), 'expressions': list(EXPRESSIONS), 'clips': {}}
    for expression in EXPRESSIONS:
        for state in ACTIVITIES:
            animation = frames(state=state, expression=expression)
            key = f'{expression}-{state}'
            sheet = Image.new('RGBA', (W * len(animation), H))
            for index, grid in enumerate(animation):
                picture = Image.new('RGBA', (W, H))
                picture.putdata([PALETTE[m] + (0 if m == BG else 255,)
                                 for row in grid for m in row])
                sheet.paste(picture, (index * W, 0))
            sheet.save(directory / f'{key}.png')
            catalog['clips'][key] = {'src': f'{key}.png', 'frames': len(animation),
                                      'poses': poses(state=state, expression=expression)}
    catalog['actions'] = list(ACTIONS)
    for name in ACTIONS:
        animation, cues = action_frames(name)
        sheet = Image.new('RGBA', (W * len(animation), H))
        for index, grid in enumerate(animation):
            picture = Image.new('RGBA', (W, H))
            picture.putdata([PALETTE[m] + (0 if m == BG else 255,) for row in grid for m in row])
            sheet.paste(picture, (index * W, 0))
        key = f'action-{name}'
        sheet.save(directory / f'{key}.png')
        catalog['clips'][key] = {'src': f'{key}.png', 'frames': len(animation), 'loop': False, 'cues': cues}
    data = json.dumps(catalog, indent=2) + '\n'
    (directory / 'manifest.json').write_text(data)
    # A plain script also works when the preview is opened directly as a local file.
    (directory / 'manifest.js').write_text('globalThis.CODSWORTH_ANIMATIONS = ' + data.rstrip() + ';\n')


def ansi(grid):
    width = len(grid[0])
    rows = grid + ([[BG] * width] if len(grid) % 2 else [])
    lines = []
    for y in range(0, len(rows), 2):
        parts = []
        for x in range(width):
            top, bottom = PALETTE[rows[y][x]], PALETTE[rows[y + 1][x]]
            parts.append(f'\x1b[38;2;{top[0]};{top[1]};{top[2]};48;2;{bottom[0]};{bottom[1]};{bottom[2]}m▀')
        lines.append(''.join(parts) + '\x1b[0m')
    return '\n'.join(lines)


def svg(grid, px=1):
    body = ''.join(
        f'<rect x="{x * px}" y="{y * px}" width="{px}" height="{px}" fill="#%02x%02x%02x"/>' % PALETTE[m]
        for y, row in enumerate(grid) for x, m in enumerate(row) if m != BG)
    width, height = len(grid[0]) * px, len(grid) * px
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" shape-rendering="crispEdges">{body}</svg>')


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('scale must be a positive integer')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--px', type=positive_int, default=6)
    parser.add_argument('--print', action='store_true')
    parser.add_argument('--ascii', action='store_true')
    parser.add_argument('--state', choices=ACTIVITIES, default='idle')
    parser.add_argument('--expression', choices=EXPRESSIONS, default='neutral')
    parser.add_argument('--catalog', action='store_true', help='also export all expression/activity sprite sheets')
    args = parser.parse_args()
    animation = frames(state=args.state, expression=args.expression)
    if args.ascii:
        print('\n'.join(''.join('.#'[m != BG] for m in row) for row in animation[0]))
        return
    if args.print:
        print(ansi(animation[0]))
        return
    if not args.out.is_dir():
        parser.error('--out must name an existing directory')

    from PIL import Image
    palette = [channel for color in PALETTE.values() for channel in color]
    palette += [0] * (768 - len(palette))
    images = []
    for grid in animation:
        image = Image.new('P', (W, H))
        image.putpalette(palette)
        image.putdata([material for row in grid for material in row])
        image.info['transparency'] = BG
        images.append(image.resize((W * args.px, H * args.px), Image.Resampling.NEAREST))
    images[0].save(args.out / 'codsworth.gif', save_all=True, append_images=images[1:],
                   duration=FRAME_MS, loop=0, transparency=BG, disposal=2, optimize=False)
    images[0].convert('RGBA').save(args.out / 'codsworth.png')
    (args.out / 'codsworth.svg').write_text(svg(animation[0], args.px))
    if args.catalog:
        export_catalog(args.out / 'animations')
    print(f'{W}x{H} grid, {len(images)} frames -> GIF / PNG / SVG in {args.out}')


if __name__ == '__main__':
    main()
