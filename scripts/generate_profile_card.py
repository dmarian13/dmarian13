import heapq
import json
import os
import random
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image, ImageFilter, ImageEnhance

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = Path(os.environ.get('INPUT_PNG', str(REPO_ROOT / 'assets' / 'github_photo.png')))
OUT_SVG = Path(os.environ.get('OUTPUT_SVG', str(REPO_ROOT / 'profile_card.svg')))

GITHUB_USER = os.environ.get('GITHUB_USER', 'dmarian13')
LINKEDIN_URL = os.environ.get('LINKEDIN_URL', 'https://www.linkedin.com/in/marian-diaconescu-3a951726a/')
LINKEDIN_SHORT = os.environ.get('LINKEDIN_SHORT', 'in/marian-diaconescu')

FALLBACK_REPOS = 13
FALLBACK_CONTRIBUTIONS = 342

LEVEL_MAP = {
    'NONE': 0,
    'FIRST_QUARTILE': 1,
    'SECOND_QUARTILE': 2,
    'THIRD_QUARTILE': 3,
    'FOURTH_QUARTILE': 4,
}


def load_fallback_heatmap():
    return [(f'2026-01-{(i % 28) + 1:02d}', 0) for i in range(365)]


def fetch_github_stats(username: str):
    """Fetch live all-time stats and last-year contribution calendar (GraphQL PAT or public HTML fallback)."""
    token = os.environ.get('GH_STATS_TOKEN') or os.environ.get('GITHUB_TOKEN')
    cur_year = datetime.now(timezone.utc).year

    if token:
        try:
            def gql(query, variables=None):
                payload = json.dumps({'query': query, 'variables': variables or {}}).encode('utf-8')
                req = urllib.request.Request(
                    'https://api.github.com/graphql',
                    data=payload,
                    headers={
                        'Authorization': f'bearer {token}',
                        'Content-Type': 'application/json',
                        'User-Agent': 'profile-card-generator',
                    },
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    return json.loads(r.read().decode('utf-8'))

            meta_res = gql(
                """
                query {
                  viewer {
                    login
                    createdAt
                    repositories(ownerAffiliations: OWNER) { totalCount }
                    contributionsCollection {
                      contributionCalendar {
                        weeks {
                          contributionDays { date contributionLevel }
                        }
                      }
                    }
                  }
                }
                """
            )
            viewer = meta_res['data']['viewer']
            use_viewer = viewer['login'].lower() == username.lower()

            if not use_viewer:
                meta_res = gql(
                    """
                    query($login: String!) {
                      user(login: $login) {
                        createdAt
                        repositories(ownerAffiliations: OWNER) { totalCount }
                        contributionsCollection {
                          contributionCalendar {
                            weeks {
                              contributionDays { date contributionLevel }
                            }
                          }
                        }
                      }
                    }
                    """,
                    {'login': username},
                )
                actor = meta_res['data']['user']
            else:
                actor = viewer

            created_year = int(actor['createdAt'][:4])
            repos = actor['repositories']['totalCount']
            heatmap = []
            for wk in actor['contributionsCollection']['contributionCalendar']['weeks']:
                for day in wk['contributionDays']:
                    heatmap.append((day['date'], LEVEL_MAP.get(day['contributionLevel'], 0)))

            total_contrib = 0
            for y in range(created_year, cur_year + 1):
                if use_viewer:
                    q = """
                    query($from: DateTime!, $to: DateTime!) {
                      viewer {
                        contributionsCollection(from: $from, to: $to) {
                          contributionCalendar { totalContributions }
                        }
                      }
                    }
                    """
                    res = gql(q, {'from': f'{y}-01-01T00:00:00Z', 'to': f'{y}-12-31T23:59:59Z'})
                    col = res['data']['viewer']['contributionsCollection']
                else:
                    q = """
                    query($login: String!, $from: DateTime!, $to: DateTime!) {
                      user(login: $login) {
                        contributionsCollection(from: $from, to: $to) {
                          contributionCalendar { totalContributions }
                        }
                      }
                    }
                    """
                    res = gql(q, {'login': username, 'from': f'{y}-01-01T00:00:00Z', 'to': f'{y}-12-31T23:59:59Z'})
                    col = res['data']['user']['contributionsCollection']
                total_contrib += col['contributionCalendar']['totalContributions']
            return repos, total_contrib, heatmap
        except Exception as exc:
            print(f"GraphQL fetch failed ({exc}); falling back...")

    try:
        def get_text(url):
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read().decode('utf-8', 'ignore')

        u = json.loads(get_text(f'https://api.github.com/users/{username}'))
        repos = u['public_repos']
        created_year = int(u['created_at'][:4])

        cal_html = get_text(f'https://github.com/users/{username}/contributions')
        raw_cells = re.findall(
            r'data-date="(\d{4}-\d{2}-\d{2})"[^>]*data-level="(\d)"|data-level="(\d)"[^>]*data-date="(\d{4}-\d{2}-\d{2})"',
            cal_html,
        )
        heatmap = sorted(((d1 or d2), int(l1 or l2)) for d1, l1, l2, d2 in raw_cells)

        total_contrib = 0
        for y in range(created_year, cur_year + 1):
            html = get_text(f'https://github.com/users/{username}/contributions?from={y}-01-01&to={y}-12-31')
            m = re.search(r'([\d,]+)\s+contributions?\s+in\s+' + str(y), html)
            if m:
                total_contrib += int(m.group(1).replace(',', ''))
        return repos, total_contrib, (heatmap or load_fallback_heatmap())
    except Exception as exc:
        print(f"Public fetch skipped/failed ({exc}); using cached defaults.")
        return FALLBACK_REPOS, FALLBACK_CONTRIBUTIONS, load_fallback_heatmap()


def wrap_pipe_items(items, max_chars: int):
    lines = []
    current = ""
    for item in items:
        candidate = item if not current else f"{current} | {item}"
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = item
    if current:
        lines.append(current)
    return lines


def xml_escape(text: str) -> str:
    return (
        text.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )


def astar_grid_order(coords, rng_seed: int):
    """Order 2D grid coordinates (r, c) using a randomized A*-like wavefront from leftmost column."""
    rng_local = random.Random(rng_seed)
    coord_set = set(coords)
    min_c = min(c for _, c in coord_set)
    seeds = [(r, c) for (r, c) in coord_set if c <= min_c + 1]

    dist = {}
    pq = []
    for sr, sc in seeds:
        cost = sc * 0.4 + rng_local.uniform(0.0, 1.2)
        dist[(sr, sc)] = cost
        heapq.heappush(pq, (cost, sr, sc))

    order = []
    visited = set()
    dirs = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.2), (0, 1, 0.75),
            (-1, -1, 1.4), (-1, 1, 1.05), (1, -1, 1.4), (1, 1, 1.05)]

    while pq:
        g_cost, r, c = heapq.heappop(pq)
        if (r, c) in visited:
            continue
        visited.add((r, c))
        order.append((r, c))
        for dr, dc, step_w in dirs:
            nr, nc = r + dr, c + dc
            if (nr, nc) in coord_set and (nr, nc) not in visited:
                new_g = g_cost + step_w * rng_local.uniform(0.55, 1.55) + max(0, nc - c) * 0.25
                if new_g < dist.get((nr, nc), 1e9):
                    dist[(nr, nc)] = new_g
                    heapq.heappush(pq, (new_g, nr, nc))

    remaining = [pt for pt in coord_set if pt not in visited]
    remaining.sort(key=lambda pt: pt[1] + rng_local.uniform(-2.0, 2.0))
    order.extend(remaining)
    return order


def make_decel_window(u: float):
    """Starts 21.25%, linear first 50%, slows smoothly to 100.0%."""
    if u <= 0.5:
        eased_u = 0.65 * u
        fade_span = 3.0
    else:
        s = (u - 0.5) / 0.5
        eased_u = 0.325 + 0.325 * s + 0.35 * (s ** 2.4)
        fade_span = 3.0 + 7.0 * (s ** 1.6)
    t_start = 21.25 + eased_u * 68.75
    t_end = min(100.0, t_start + fade_span)
    return t_start, t_end


REPOS_COUNT, ALL_TIME_CONTRIBS, HEATMAP_DAYS = fetch_github_stats(GITHUB_USER)

LANGUAGES_VAL = "C/C++ | Python | SQL | Java | VHDL | ELM"
TOOLS_ITEMS = ["Git", "Docker", "Linux", "PostgreSQL", "Zed", "AMD Vivado", "AMD Vitis", "STM32CubeIDE"]
TOOLS_LINES = wrap_pipe_items(TOOLS_ITEMS, max_chars=len(LANGUAGES_VAL))

PROFILE_HEADER = f"{GITHUB_USER}@github"
PROFILE_SECTIONS = [
    ("Status", "Computer engineering student", None),
    ("Languages", LANGUAGES_VAL, None),
    ("Tools", TOOLS_LINES, None),
    None,
    ("GitHub Stats", None, None),
    ("Repos", str(REPOS_COUNT), None),
    ("Contributions", str(ALL_TIME_CONTRIBS), None),
    None,
    ("Contact", None, None),
    ("LinkedIn", LINKEDIN_SHORT, LINKEDIN_URL),
    ("Email", "mariandiaconescu5@gmail.com", None),
]

W, H = 920, 442
TOTAL_DUR = 0.81

BG_CARD = "#0d1117"
BORDER_COL = "#262c34"
DOT_COLOR = "#b8c2cc"
NN_COLOR = "#d0d7de"

HEATMAP_COLORS = {
    0: "#161b22",
    1: "#39414a",
    2: "#636c76",
    3: "#9aa4b0",
    4: "#e6edf3",
}

LEFT_PANEL_W = 296
IMG_MAX_W, IMG_MAX_H = 205, 268
STEP = 2.9

im = Image.open(SRC).convert('RGBA')
crop = im.crop(im.getbbox())

scale = min(IMG_MAX_W / crop.width, IMG_MAX_H / crop.height)
target_w = crop.width * scale
target_h = crop.height * scale

cols = int(round(target_w / STEP))
rows = int(round(target_h / STEP))

resized = crop.resize((cols, rows), Image.Resampling.LANCZOS)
alpha_img = resized.split()[3]
gray_img = resized.convert('RGB').convert('L')

a_data = list(alpha_img.getdata())
g_data = list(gray_img.getdata())
subj_vals = sorted(g for g, a in zip(g_data, a_data) if a >= 100)
lo = subj_vals[int(0.05 * len(subj_vals))]
hi = subj_vals[int(0.97 * len(subj_vals))]
span = max(1, hi - lo)

alpha_edges = alpha_img.filter(ImageFilter.FIND_EDGES)
lum_edges = ImageEnhance.Contrast(gray_img).enhance(1.8).filter(ImageFilter.FIND_EDGES)

alpha_px = alpha_img.load()
gray_px = gray_img.load()
aedge_px = alpha_edges.load()
ledge_px = lum_edges.load()

offset_x = (LEFT_PANEL_W - cols * STEP) / 2.0
offset_y = (H - rows * STEP) / 2.0
BAYER2 = ((0.0, 0.5), (0.75, 0.25))

active_dots = {}
for r in range(rows):
    for c in range(cols):
        a = alpha_px[c, r]
        if a < 95:
            continue
        lum = max(0.0, min(1.0, (gray_px[c, r] - lo) / span)) ** 0.78
        sil_edge = min(1.0, aedge_px[c, r] / 180.0)
        int_edge = min(1.0, ledge_px[c, r] / 140.0)
        score = min(1.0, max(0.20 + 0.80 * lum, 0.72 * sil_edge, 0.62 * int_edge) * (a / 255.0))
        bayer = BAYER2[r & 1][c & 1]
        if score < 0.28 and score < 0.15 + 0.16 * bayer:
            continue

        radius = 0.28 + 0.60 * score
        opacity = 0.36 + 0.54 * score
        cx = offset_x + (c + 0.5) * STEP
        cy = offset_y + (r + 0.5) * STEP
        active_dots[(r, c)] = (cx, cy, radius, opacity)

order = astar_grid_order(active_dots.keys(), rng_seed=42)
N_IMG_STEPS = 72
img_buckets = {i: [] for i in range(N_IMG_STEPS)}
total_active = max(1, len(order))
for idx, pt in enumerate(order):
    b = min(N_IMG_STEPS - 1, int((idx / total_active) * N_IMG_STEPS))
    img_buckets[b].append(active_dots[pt])

RIGHT_X = LEFT_PANEL_W + 30
NN_X0, NN_X1 = RIGHT_X + 16, W - 38
NN_Y0, NN_Y1 = 26, 330

LAYER_SIZES = [22, 20, 18, 16, 14, 13, 11, 10, 9, 8]
n_layers = len(LAYER_SIZES)

rng = random.Random(42)
T_NN_START = 0.5
nn_nodes = []
max_nodes = LAYER_SIZES[0]
full_span_y = NN_Y1 - NN_Y0
mid_y = (NN_Y0 + NN_Y1) / 2.0

for li, count in enumerate(LAYER_SIZES):
    lx = NN_X0 + (NN_X1 - NN_X0) * (li / (n_layers - 1))
    used_h = full_span_y * ((count / max_nodes) ** 1.15)
    y_start = mid_y - used_h / 2.0
    layer_list = []
    base_layer_t = T_NN_START + (li / (n_layers - 1)) * 74.0
    for ni in range(count):
        ly = y_start + (used_h * ni / max(1, count - 1)) if count > 1 else mid_y
        node_jitter = rng.uniform(-2.8, 3.8)
        fire_t = max(0.5, min(78.5, base_layer_t + node_jitter))
        layer_list.append((lx, ly, fire_t))
    nn_nodes.append(layer_list)

nn_edges = []
for li in range(n_layers - 1):
    cur_layer = nn_nodes[li]
    nxt_layer = nn_nodes[li + 1]
    n1, n2 = len(cur_layer), len(nxt_layer)
    for i, (x1, y1, t1) in enumerate(cur_layer):
        center_j = (i / max(1, n1 - 1)) * (n2 - 1)
        targets = set()
        for j in range(n2):
            if abs(j - center_j) <= 1.25:
                targets.add(j)
        targets.add(rng.randrange(n2))
        for j in sorted(targets):
            x2, y2, t2 = nxt_layer[j]
            edge_t = 0.5 * (t1 + t2) + rng.uniform(-1.0, 1.0)
            nn_edges.append((x1, y1, x2, y2, edge_t))

N_NN_BUCKETS = 56
nn_node_buckets = {i: [] for i in range(N_NN_BUCKETS)}
nn_edge_buckets = {i: [] for i in range(N_NN_BUCKETS)}

T_BUCKET_MIN, T_BUCKET_MAX = 0.5, 79.0
for layer in nn_nodes:
    for nx, ny, ft in layer:
        idx = int(((ft - T_BUCKET_MIN) / (T_BUCKET_MAX - T_BUCKET_MIN)) * (N_NN_BUCKETS - 1))
        idx = max(0, min(N_NN_BUCKETS - 1, idx))
        nn_node_buckets[idx].append((nx, ny))

for x1, y1, x2, y2, ft in nn_edges:
    idx = int(((ft - T_BUCKET_MIN) / (T_BUCKET_MAX - T_BUCKET_MIN)) * (N_NN_BUCKETS - 1))
    idx = max(0, min(N_NN_BUCKETS - 1, idx))
    nn_edge_buckets[idx].append((x1, y1, x2, y2))

HEAT_Y = 342
HEAT_PITCH = 10.6
HEAT_CELL = 8.4

first_dt = datetime.strptime(HEATMAP_DAYS[0][0], '%Y-%m-%d')
start_row = (first_dt.weekday() + 1) % 7

commit_cells = []
empty_cells = []
for idx, (d_str, level) in enumerate(HEATMAP_DAYS):
    slot = start_row + idx
    col = slot // 7
    row = slot % 7
    cx = RIGHT_X + col * HEAT_PITCH
    cy = HEAT_Y + row * HEAT_PITCH
    if level > 0:
        commit_cells.append((cx, cy, level))
    else:
        empty_cells.append((cx, cy, 0))

heat_rng = random.Random(314)
heat_rng.shuffle(commit_cells)

N_COMMIT_STEPS = min(64, max(1, len(commit_cells)))
commit_buckets = {i: [] for i in range(N_COMMIT_STEPS)}
for idx, cell in enumerate(commit_cells):
    b = min(N_COMMIT_STEPS - 1, int((idx / max(1, len(commit_cells))) * N_COMMIT_STEPS))
    commit_buckets[b].append(cell)

heat_rng.shuffle(empty_cells)
N_EMPTY_STEPS = 6
empty_buckets = {i: [] for i in range(N_EMPTY_STEPS)}
for idx, cell in enumerate(empty_cells):
    b = min(N_EMPTY_STEPS - 1, int((idx / max(1, len(empty_cells))) * N_EMPTY_STEPS))
    empty_buckets[b].append(cell)

css = [
    ".mono { font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, 'Liberation Mono', monospace; }",
    ".link { text-decoration: underline; text-decoration-color: #6e7681; }",
    ".link:hover { fill: #ffffff; text-decoration-color: #ffffff; }",
]

for b in range(N_IMG_STEPS):
    if not img_buckets[b]:
        continue
    t_start, t_end = make_decel_window(b / (N_IMG_STEPS - 1))
    css.append(f".ib{b}{{animation:kib{b} {TOTAL_DUR}s ease-out forwards;}}")
    css.append(f"@keyframes kib{b}{{0%,{t_start:.2f}%{{opacity:0}}{t_end:.2f}%,100%{{opacity:1}}}}")

for b in range(N_COMMIT_STEPS):
    if not commit_buckets[b]:
        continue
    u = b / max(1, N_COMMIT_STEPS - 1)
    t_pct = 8.0 + u * 69.0
    t_snap = t_pct + 0.5
    css.append(f".hc{b}{{animation:khc{b} {TOTAL_DUR}s steps(1,end) forwards;}}")
    css.append(f"@keyframes khc{b}{{0%,{t_pct:.2f}%{{opacity:0}}{t_snap:.2f}%,100%{{opacity:1}}}}")

for b in range(N_EMPTY_STEPS):
    if not empty_buckets[b]:
        continue
    t_on = 79.0 + b * 2.0
    t_end = min(99.0, t_on + 9.0)
    css.append(f".he{b}{{animation:khe{b} {TOTAL_DUR}s ease-out forwards;}}")
    css.append(f"@keyframes khe{b}{{0%,{t_on:.2f}%{{opacity:0}}{t_end:.2f}%,100%{{opacity:1}}}}")

for b in range(N_NN_BUCKETS):
    t_on = T_BUCKET_MIN + (b / (N_NN_BUCKETS - 1)) * (T_BUCKET_MAX - T_BUCKET_MIN)
    t_peak = t_on + 3.2
    t_off = min(91.0, t_on + 11.5)
    if nn_node_buckets[b]:
        css.append(f".nn{b}{{animation:knn{b} {TOTAL_DUR}s linear forwards;}}")
        css.append(f"@keyframes knn{b}{{0%,{t_on:.2f}%{{opacity:0}}{t_peak:.2f}%{{opacity:0.92}}{t_off:.2f}%,100%{{opacity:0}}}}")
    if nn_edge_buckets[b]:
        css.append(f".ne{b}{{animation:kne{b} {TOTAL_DUR}s linear forwards;}}")
        css.append(f"@keyframes kne{b}{{0%,{t_on:.2f}%{{opacity:0}}{t_peak:.2f}%{{opacity:0.38}}{t_off:.2f}%,100%{{opacity:0}}}}")

rows_meta = []
y_cursor = 52
rows_meta.append(("header", y_cursor, PROFILE_HEADER, None, None))
y_cursor += 30
for item in PROFILE_SECTIONS:
    if item is None:
        y_cursor += 12
        continue
    k, v, href = item
    if v is None:
        rows_meta.append(("section", y_cursor, k, None, None))
        y_cursor += 24
    elif isinstance(v, list):
        rows_meta.append(("kv_multi", y_cursor, k, v, href))
        y_cursor += 20 + 24 * (len(v) - 1)
    else:
        rows_meta.append(("kv", y_cursor, k, v, href))
        y_cursor += 24

row_rng = random.Random(99)
row_windows = []

layer_bounds = []
for li, layer in enumerate(nn_nodes):
    ys = [ny for _, ny, _ in layer]
    y_min, y_max = min(ys) - 8.0, max(ys) + 8.0
    t_clear = max(ft for _, _, ft in layer) + 11.5
    layer_bounds.append((li, y_min, y_max, min(91.0, t_clear)))

for ri, (rtype, ry, _, _, _) in enumerate(rows_meta):
    overlapping = [lb for lb in layer_bounds if lb[1] <= ry <= lb[2]]
    if overlapping:
        max_li, _, _, _ = overlapping[-1]
    else:
        max_li = 0

    overlap_frac = max_li / max(1, n_layers - 1)
    r_start = 11.5 + 12.5 * overlap_frac + row_rng.uniform(-1.5, 2.5)
    r_end = 78.5 + 19.5 * (overlap_frac ** 0.9) + row_rng.uniform(-1.5, 2.0)
    r_start = max(10.0, r_start)
    r_end = max(r_start + 55.0, min(99.5, r_end))
    row_windows.append([r_start, r_end])

max_ri = max(range(len(row_windows)), key=lambda idx: row_windows[idx][1])
row_windows[max_ri][1] = 100.0

for ri, (r_start, r_end) in enumerate(row_windows):
    css.append(f".nr{ri}{{animation:knr{ri} {TOTAL_DUR}s linear forwards;}}")
    css.append(
        f"@keyframes knr{ri}{{0%,{r_start:.1f}%{{clip-path:inset(0 100% 0 0)}}{r_end:.1f}%,100%{{clip-path:inset(0 0% 0 0)}}}}"
    )

parts = [
    f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="dmarian13 GitHub Neofetch Profile">',
    "<style>\n" + "\n".join(css) + "\n</style>",
    f'<rect x="0.5" y="0.5" width="{W-1}" height="{H-1}" rx="12" fill="{BG_CARD}" stroke="{BORDER_COL}" stroke-width="1"/>',
    f'<line x1="{LEFT_PANEL_W + 4}" y1="24" x2="{LEFT_PANEL_W + 4}" y2="{H - 24}" stroke="#21262d" stroke-width="1"/>',
]

parts.append(f'<g fill="{DOT_COLOR}">')
for b in range(N_IMG_STEPS):
    dots = img_buckets[b]
    if not dots:
        continue
    parts.append(f'<g class="ib{b}">')
    for cx, cy, rad, op in dots:
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{rad:.2f}" fill-opacity="{op:.2f}"/>')
    parts.append("</g>")
parts.append("</g>")

VAL_X = W - 34
KEY_X = RIGHT_X
CHAR_W_125 = 7.5
for ri, (rtype, y, k, v, href) in enumerate(rows_meta):
    parts.append(f'<g class="nr{ri}">')
    if rtype == "header":
        parts.append(
            f'<text x="{KEY_X}" y="{y}" class="mono" font-size="14" font-weight="700" fill="#f0f6fc">{xml_escape(k)}</text>'
        )
        line_start = KEY_X + len(k) * 8.6 + 8
        parts.append(
            f'<line x1="{line_start:.1f}" y1="{y-4}" x2="{VAL_X}" y2="{y-4}" stroke="#30363d" stroke-width="1" stroke-dasharray="3 4"/>'
        )
    elif rtype == "section":
        label = f"- {k}"
        parts.append(
            f'<text x="{KEY_X}" y="{y}" class="mono" font-size="12.5" font-weight="700" fill="#c9d1d9">{xml_escape(label)}</text>'
        )
        line_start = KEY_X + len(label) * CHAR_W_125 + 8
        parts.append(
            f'<line x1="{line_start:.1f}" y1="{y-4}" x2="{VAL_X}" y2="{y-4}" stroke="#21262d" stroke-width="1" stroke-dasharray="2 5"/>'
        )
    elif rtype == "kv":
        label = f"{k}:"
        parts.append(
            f'<text x="{KEY_X}" y="{y}" class="mono" font-size="12.5" font-weight="600" fill="#8b949e">{xml_escape(label)}</text>'
        )
        dots_x0 = KEY_X + len(label) * CHAR_W_125 + 6
        dots_x1 = VAL_X - len(v) * CHAR_W_125 - 8
        if dots_x1 > dots_x0 + 8:
            parts.append(
                f'<line x1="{dots_x0:.1f}" y1="{y-4}" x2="{dots_x1:.1f}" y2="{y-4}" stroke="#2b3138" stroke-width="1" stroke-dasharray="1.5 5"/>'
            )
        val_txt = xml_escape(v)
        if href:
            safe_href = xml_escape(href)
            parts.append(
                f'<a href="{safe_href}" xlink:href="{safe_href}" target="_blank">'
                f'<text x="{VAL_X}" y="{y}" text-anchor="end" class="mono link" font-size="12.5" fill="#e6edf3">{val_txt}</text>'
                f'</a>'
            )
        else:
            parts.append(
                f'<text x="{VAL_X}" y="{y}" text-anchor="end" class="mono" font-size="12.5" fill="#e6edf3">{val_txt}</text>'
            )
    elif rtype == "kv_multi":
        label = f"{k}:"
        parts.append(
            f'<text x="{KEY_X}" y="{y}" class="mono" font-size="12.5" font-weight="600" fill="#8b949e">{xml_escape(label)}</text>'
        )
        dots_x0 = KEY_X + len(label) * CHAR_W_125 + 6
        dots_x1 = VAL_X - len(v[0]) * CHAR_W_125 - 8
        if dots_x1 > dots_x0 + 8:
            parts.append(
                f'<line x1="{dots_x0:.1f}" y1="{y-4}" x2="{dots_x1:.1f}" y2="{y-4}" stroke="#2b3138" stroke-width="1" stroke-dasharray="1.5 5"/>'
            )
        for idx_line, line_txt in enumerate(v):
            ly = y + idx_line * 20
            parts.append(
                f'<text x="{VAL_X}" y="{ly}" text-anchor="end" class="mono" font-size="12.5" fill="#e6edf3">{xml_escape(line_txt)}</text>'
            )
    parts.append("</g>")

parts.append('<g id="contrib-heatmap">')
for b in range(N_EMPTY_STEPS):
    cells = empty_buckets[b]
    if not cells:
        continue
    parts.append(f'<g class="he{b}" fill="{HEATMAP_COLORS[0]}">')
    for hx, hy, _ in cells:
        parts.append(f'<rect x="{hx:.1f}" y="{hy:.1f}" width="{HEAT_CELL}" height="{HEAT_CELL}" rx="1.8"/>')
    parts.append("</g>")

for b in range(N_COMMIT_STEPS):
    cells = commit_buckets[b]
    if not cells:
        continue
    parts.append(f'<g class="hc{b}">')
    for hx, hy, lvl in cells:
        col = HEATMAP_COLORS.get(lvl, HEATMAP_COLORS[1])
        parts.append(f'<rect x="{hx:.1f}" y="{hy:.1f}" width="{HEAT_CELL}" height="{HEAT_CELL}" rx="1.8" fill="{col}"/>')
    parts.append("</g>")
parts.append("</g>")

parts.append('<g id="nn-overlay">')
for b in range(N_NN_BUCKETS):
    edges = nn_edge_buckets[b]
    if not edges:
        continue
    parts.append(f'<g class="ne{b}" opacity="0" stroke="{NN_COLOR}" stroke-width="0.6" stroke-linecap="round">')
    for x1, y1, x2, y2 in edges:
        parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>')
    parts.append("</g>")

for b in range(N_NN_BUCKETS):
    nodes = nn_node_buckets[b]
    if not nodes:
        continue
    parts.append(f'<g class="nn{b}" opacity="0">')
    for nx, ny in nodes:
        parts.append(f'<circle cx="{nx:.1f}" cy="{ny:.1f}" r="4.0" fill="{NN_COLOR}" fill-opacity="0.16"/>')
        parts.append(f'<circle cx="{nx:.1f}" cy="{ny:.1f}" r="2.0" fill="{NN_COLOR}"/>')
    parts.append("</g>")
parts.append("</g>")

parts.append("</svg>")
OUT_SVG.write_text("\n".join(parts), encoding="utf-8")
print(f"Generated {OUT_SVG}: repos={REPOS_COUNT}, contribs={ALL_TIME_CONTRIBS}, commit_cells={len(commit_cells)}, empty_cells={len(empty_cells)}, size={OUT_SVG.stat().st_size} bytes")
