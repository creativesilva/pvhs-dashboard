#!/usr/bin/env python3
"""
PVHS Dashboard Server
- Firebase Auth (Google sign-in) verification
- Canvas LMS API proxy (GET/POST/PUT/DELETE)
- Batch operations (grades, comments)
- Static file serving
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse
import http.client
import ssl
import json
import os
import sys
import mimetypes
import time
import base64
import gzip
import threading

sys.stdout.reconfigure(line_buffering=True)

PORT         = int(os.environ.get('PORT', 8080))
CANVAS_HOST  = 'smjuhsd.instructure.com'
CANVAS_TOKEN = os.environ.get('CANVAS_TOKEN', '').strip()
API_KEY      = os.environ.get('API_KEY', '')
# Camera-reservation eligibility: a Silva PHOTO student with MORE THAN this many missing
# assignments (in Silva's Photo Canvas courses) is blocked from reserving until they turn work in.
# Garcia's students are never gated (they are not in Silva's courses, and we skip the check for them).
# The check is LIVE against Canvas at ID entry and FAILS OPEN: any error/undetermined => allowed.
MISSING_LIMIT = int(os.environ.get('MISSING_LIMIT', '6'))
# Shared camera-manager password. Unlocks ONLY the camera checkout endpoints
# (not Canvas, grades, or the full roster) so another teacher (e.g. Ms. Garcia)
# can run the standalone camera calendar without a Command Center login.
CAMERA_PIN   = os.environ.get('CAMERA_PIN', '').strip()
CAMERA_OVERRIDE_CODE = os.environ.get('CAMERA_OVERRIDE_CODE', '3duc4t10n').strip()   # teacher code to unlock a Photo-2 camera for a Photo-1 student
SERVE_DIR    = os.path.dirname(os.path.abspath(__file__))

FIREBASE_PROJECT_ID = 'girl-scouts-silva'
ALLOWED_EMAILS = [
    e.strip().lower()
    for e in os.environ.get('ALLOWED_EMAILS', '').split(',')
    if e.strip()
]

# Auto-load roster data from ROSTER_DATA env var (base64+gzip compressed)
_roster_env = os.environ.get('ROSTER_DATA', '')
if _roster_env:
    _roster_path = os.path.join(SERVE_DIR, 'roster_data.json')
    if not os.path.exists(_roster_path):
        try:
            raw = gzip.decompress(base64.b64decode(_roster_env))
            with open(_roster_path, 'wb') as f:
                f.write(raw)
            data = json.loads(raw)
            print(f'[roster] Auto-loaded from env: {len(data["students"])} students, {len(data["sections"])} sections')
        except Exception as e:
            print(f'[roster] Failed to load from env: {e}')

# Garcia's CAMERA-ONLY roster (Mrs. Garcia / Solorio, CTE Photo 1A P2+P3): students that exist ONLY
# for camera-checkout resolution (resolve_student), never in Silva's dashboard roster/grading. It is
# loaded from the persistent disk (uploaded via the camera-scoped endpoint), or seeded from the
# GARCIA_ROSTER env var. Populated by _load_garcia() once the data dir is known (below).
_GARCIA_STUDENTS = []

# ---------------------------------------------------------------------------
# Camera checkout: durable storage on the Render disk (/var/data), roster lookup
# ---------------------------------------------------------------------------

# Camera inventory. CAMERAS (Cam 01..18) is the shared Silva/Garcia set (16-18 are Photo-2-only).
# Ms. Mankin (Digital Arts) has her OWN Cam 19-21. ALL_CAMERAS is the full known set used for
# checkout/asset validity; CAMERAS stays 01..18 so the existing feeds/clients are unchanged.
CAMERAS = ["Cam 01","Cam 02","Cam 03","Cam 04","Cam 05","Cam 06","Cam 07","Cam 08","Cam 09",
           "Cam 10","Cam 11","Cam 12","Cam 13","Cam 14","Cam 15","Cam 16","Cam 17","Cam 18"]
MANKIN_CAMERAS = ["Cam 19","Cam 20","Cam 21"]
ALL_CAMERAS = CAMERAS + MANKIN_CAMERAS
# Per-teacher camera pools the consoles + student calendar curate by. Silva: all 01-18. Garcia:
# 01-15 (shares Silva's cameras, no Photo-2 kit 16-18). Mankin: her own 19-21.
CAMERA_POOLS = {
    "silva":  list(CAMERAS),
    "garcia": CAMERAS[:15],
    "mankin": list(MANKIN_CAMERAS),
}

# Add-on gear, tracked as its OWN PER-UNIT checkout records (kind='equipment', item=<unit id>) so
# every physical unit reserves, picks up, returns, is noted, out-of-serviced, and kit-checked
# independently, exactly like a camera. `pool`: "shared" = Silva + Garcia (Photo 1 + Photo 2);
# "mankin" = Ms. Mankin's own gear.
EQUIPMENT_TYPES = {
    # key: {label (type name), unit (per-unit label prefix), count, pool, start?(default 1), kit[]}
    "wide":      {"label": "Ultra-Wide Lens",  "unit": "Ultra-Wide", "count": 5,  "pool": "shared",
                  "kit": ["Front lens cap", "Rear lens cap", "Lens filter", "Lens hood"]},
    "zoom":      {"label": "Ultra-Zoom Lens",  "unit": "Ultra-Zoom", "count": 2,  "pool": "shared",
                  "kit": ["Front lens cap", "Rear lens cap", "Lens filter", "Lens hood"]},
    "speedlite": {"label": "Speedlite",        "unit": "Speedlite",  "count": 5,  "pool": "shared",
                  "kit": ["Diffuser dome", "Mini stand", "Soft pouch"]},
    "tripod":    {"label": "Tripod",           "unit": "Tripod",     "count": 5,  "pool": "shared",
                  "kit": ["Quick-release plate", "Carry bag"]},
    "reflector": {"label": "Neewer Reflector", "unit": "Reflector",  "count": 5,  "pool": "shared",
                  "kit": ["Scrim frame", "Reflector zip surface", "Reflector zip case"]},
    "mankin_tripod": {"label": "Tripod (Mankin)", "unit": "Mankin Tripod", "count": 3, "pool": "mankin",
                  "kit": ["Quick-release plate", "Carry bag"]},
    "apple_pencil":  {"label": "iPad Stylus", "unit": "iPad Stylus", "count": 10, "pool": "mankin", "start": 31},
}
# Expand the types into concrete per-unit identities: id "<type>-<NN>" -> {type, label, pool, kit}.
EQUIPMENT_UNITS = {}
EQUIPMENT_UNIT_IDS = []
for _t, _d in EQUIPMENT_TYPES.items():
    _start = _d.get("start", 1)
    for _i in range(_start, _start + _d["count"]):
        _uid = "%s-%02d" % (_t, _i)
        EQUIPMENT_UNITS[_uid] = {"type": _t, "label": "%s %02d" % (_d["unit"], _i),
                                 "pool": _d["pool"], "kit": list(_d.get("kit", []))}
        EQUIPMENT_UNIT_IDS.append(_uid)
# Per-teacher gear views: unit ids grouped by pool. "shared" units appear for Silva and Garcia.
EQUIPMENT_POOLS = {
    "silva":  [u for u in EQUIPMENT_UNIT_IDS if EQUIPMENT_UNITS[u]["pool"] == "shared"],
    "garcia": [u for u in EQUIPMENT_UNIT_IDS if EQUIPMENT_UNITS[u]["pool"] == "shared"],
    "mankin": [u for u in EQUIPMENT_UNIT_IDS if EQUIPMENT_UNITS[u]["pool"] == "mankin"],
}
# Type-level labels for grouping units under a header in the clients.
EQUIPMENT_TYPE_LABELS = {t: {"label": d["label"], "unit": d["unit"]} for t, d in EQUIPMENT_TYPES.items()}

def _data_dir():
    """The Render persistent disk mount, or the app dir as a local/dev fallback."""
    for d in ('/var/data', '/data'):
        if os.path.isdir(d):
            return d
    return SERVE_DIR

CHECKOUTS_PATH = os.path.join(_data_dir(), 'checkouts.json')
_checkouts_lock = threading.Lock()

# Garcia camera-only roster: persistent-disk file (uploaded via camera endpoint) wins; else seed
# from the GARCIA_ROSTER env var and persist it to disk so it survives redeploys.
GARCIA_PATH = os.path.join(_data_dir(), 'garcia_roster.json')
_garcia_lock = threading.Lock()

def _load_garcia():
    global _GARCIA_STUDENTS
    lst = []
    if os.path.exists(GARCIA_PATH):
        try:
            lst = json.load(open(GARCIA_PATH)).get('students', [])
        except Exception as e:
            print(f'[garcia] disk load failed: {e}')
    elif os.environ.get('GARCIA_ROSTER'):
        try:
            gd = json.loads(gzip.decompress(base64.b64decode(os.environ['GARCIA_ROSTER'])))
            lst = gd.get('students', gd) if isinstance(gd, dict) else gd
            with open(GARCIA_PATH, 'w') as f:
                json.dump({'students': lst}, f)
        except Exception as e:
            print(f'[garcia] env seed failed: {e}')
    _GARCIA_STUDENTS = lst
    print(f'[garcia] {len(lst)} camera-only photography students loaded')
    return lst

# Ms. Mankin's Digital Arts camera roster (her own cameras 19-21 + gear). Same pattern as Garcia:
# a persistent-disk file wins, else seed from the MANKIN_ROSTER env var, then persist to disk.
_MANKIN_STUDENTS = []
MANKIN_PATH = os.path.join(_data_dir(), 'mankin_roster.json')
_mankin_lock = threading.Lock()

def _load_mankin():
    global _MANKIN_STUDENTS
    lst = []
    if os.path.exists(MANKIN_PATH):
        try:
            lst = json.load(open(MANKIN_PATH)).get('students', [])
        except Exception as e:
            print(f'[mankin] disk load failed: {e}')
    elif os.environ.get('MANKIN_ROSTER'):
        try:
            gd = json.loads(gzip.decompress(base64.b64decode(os.environ['MANKIN_ROSTER'])))
            lst = gd.get('students', gd) if isinstance(gd, dict) else gd
            with open(MANKIN_PATH, 'w') as f:
                json.dump({'students': lst}, f)
        except Exception as e:
            print(f'[mankin] env seed failed: {e}')
    _MANKIN_STUDENTS = lst
    print(f'[mankin] {len(lst)} digital-arts camera students loaded')
    return lst

_load_garcia()
_load_mankin()

# Persistent student-contact OVERRIDES (student self-edits + teacher edits). Kept on the disk,
# separate from the base rosters, and merged on top in _student_result so a correction sticks
# across redeploys and is fully reversible (delete the entry to revert to the roster value).
OVERRIDES_PATH = os.path.join(_data_dir(), 'roster_overrides.json')
_overrides_lock = threading.Lock()
_ROSTER_OVERRIDES = {}

def _load_overrides():
    global _ROSTER_OVERRIDES
    d = {}
    if os.path.exists(OVERRIDES_PATH):
        try:
            d = json.load(open(OVERRIDES_PATH))
        except Exception as e:
            print(f'[overrides] load failed: {e}')
    _ROSTER_OVERRIDES = d if isinstance(d, dict) else {}
    print(f'[overrides] {len(_ROSTER_OVERRIDES)} student contact overrides loaded')
    return _ROSTER_OVERRIDES

def _save_overrides():
    with open(OVERRIDES_PATH, 'w') as f:
        json.dump(_ROSTER_OVERRIDES, f)

def _clean_phone(v):
    return ''.join(ch for ch in str(v or '') if ch in '0123456789 ()+-.').strip()[:20]

def _clean_name(v):
    return ' '.join(str(v or '').split())[:60]

def _write_override(sid, data, ip=''):
    """Merge contact corrections into the persistent overrides store for ONE student, so a fix
    sticks on the student's profile across redeploys and is fully reversible. Only contact fields
    are written; base rosters are never touched. Returns True if anything was written."""
    sid = str(sid).strip()
    fields = {}
    if 'student_cell' in data:    fields['student_cell'] = _clean_phone(data.get('student_cell'))
    if 'parent_guardian' in data: fields['parent_guardian'] = _clean_name(data.get('parent_guardian'))
    if 'parent_cell' in data:     fields['parent_cell'] = _clean_phone(data.get('parent_cell'))
    if not fields:
        return False
    with _overrides_lock:
        cur = _ROSTER_OVERRIDES.get(sid, {})
        cur.update(fields)
        cur['updated'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        if ip:
            cur['_ip'] = ip
        _ROSTER_OVERRIDES[sid] = cur
        _save_overrides()
    return True

_load_overrides()

def load_checkouts():
    try:
        with open(CHECKOUTS_PATH) as f:
            return json.load(f)
    except Exception:
        return []

def save_checkouts(items):
    tmp = CHECKOUTS_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(items, f)
    os.replace(tmp, CHECKOUTS_PATH)

# Photo Walk logs: Mr. Silva's per-period record of which kit each seat took on a given day, with the
# kit's completion % at that moment. SILVA-only, authenticated, server-side (ties kits to students).
# Keyed "YYYY-MM-DD|<period>". One log per date+period (reopening a day loads it to adjust).
PHOTOWALK_PATH = os.path.join(_data_dir(), 'photo_walk_logs.json')
_photowalk_lock = threading.Lock()

def load_photowalks():
    try:
        with open(PHOTOWALK_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def save_photowalks(d):
    tmp = PHOTOWALK_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(d, f)
    os.replace(tmp, PHOTOWALK_PATH)

# Per-camera ASSET store: a standing note that follows the physical camera across checkouts
# (e.g. "lens cap replaced 9/28", "small scratch") + an out-of-service flag for broken gear.
# Keyed by camera name (Cam 01..18). Separate from per-checkout notes, which are incident history.
ASSETS_PATH = os.path.join(_data_dir(), 'camera_assets.json')
_assets_lock = threading.Lock()

def load_assets():
    try:
        with open(ASSETS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def save_assets(data):
    tmp = ASSETS_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, ASSETS_PATH)

# Memory-card wallet: 18 physical slots. When a student returns a camera in a hurry without
# offloading, we pull the SD card into a numbered wallet slot (and flag the camera "no card")
# so it does not go back out until a fresh card is installed. The held card clears once the
# student comes back, offloads, and we return it. Keyed by slot string "1".."18".
MEMCARDS_PATH = os.path.join(_data_dir(), 'memory_cards.json')
_memcards_lock = threading.Lock()
CARD_SLOTS = 18

def load_memcards():
    try:
        with open(MEMCARDS_PATH) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def save_memcards(data):
    tmp = MEMCARDS_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, MEMCARDS_PATH)

# Teacher-declared BLACKOUT days: days the teacher closes to student camera checkouts (e.g. an
# absence). Keyed by ISO date "YYYY-MM-DD" -> {added}. NO reason is stored or shown to students;
# a closed day is simply grayed and not reservable. Exposed on both feeds.
BLACKOUTS_PATH = os.path.join(_data_dir(), 'blackout_days.json')
_blackouts_lock = threading.Lock()

def _load_blackouts():
    try:
        with open(BLACKOUTS_PATH) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _save_blackouts(data):
    tmp = BLACKOUTS_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, BLACKOUTS_PATH)

def _blackout_list():
    return sorted(_load_blackouts().keys())

def _is_blackout(iso):
    return str(iso or '') in _load_blackouts()

# Lifecycle: reserved -> out (picked up) -> returned. Records saved before this
# field existed have no 'status', so derive it: returned => returned, else out
# (they were migrated from the calendar as cameras already in students' hands).
def status_of(c):
    s = (c.get('status') or '').strip().lower()
    if s in ('reserved', 'out', 'returned'):
        return s
    return 'returned' if c.get('returned') else 'out'

# Set a checkout's status and keep the pickup/return date stamps consistent.
def stamp_status(c, new):
    new = (new or '').strip().lower()
    if new not in ('reserved', 'out', 'returned'):
        new = 'reserved'
    c['status'] = new
    if new == 'reserved':
        c['picked_up_date'] = ''
        c['returned'] = False
        c['returned_date'] = ''
    elif new == 'out':
        c['picked_up_date'] = c.get('picked_up_date') or time.strftime('%Y-%m-%d')
        c['returned'] = False
        c['returned_date'] = ''
    elif new == 'returned':
        c['returned'] = True
        c['returned_date'] = c.get('returned_date') or time.strftime('%Y-%m-%d')
    return new

# Light per-IP rate limit for the public ID-confirm lookup (deters roster harvesting).
_lookup_hits = {}
_lookup_lock = threading.Lock()
def _rate_ok(ip, limit=40, window=60):
    now = time.time()
    with _lookup_lock:
        hits = [t for t in _lookup_hits.get(ip, []) if now - t < window]
        if len(hits) >= limit:
            _lookup_hits[ip] = hits
            return False
        hits.append(now)
        _lookup_hits[ip] = hits
        return True

def _roster_students():
    try:
        with open(os.path.join(SERVE_DIR, 'roster_data.json')) as f:
            return json.load(f).get('students', [])
    except Exception:
        return []

def _is_photo(s):
    """A photography student (CTE Photo 1 or 2). Digital Arts students are NOT photo."""
    v = ((s.get('course_code') or '') + ' ' + (s.get('course') or '')).lower()
    return 'photo' in v

def _camera_students():
    """Roster the camera checkout resolves against: PHOTOGRAPHY students only.
    = Silva's Photo classes (Digital Arts EXCLUDED, handled on paper) + Garcia's Photo roster.
    Garcia is listed first so a student in both (Silva DA + Garcia Photo) resolves as Photo."""
    return list(_GARCIA_STUDENTS) + list(_MANKIN_STUDENTS) + [s for s in _roster_students() if _is_photo(s)]

def _pool_for(teacher):
    """Map a resolved teacher name to a camera-pool key (silva / garcia / mankin)."""
    t = (teacher or '').lower()
    if 'garcia' in t: return 'garcia'
    if 'mankin' in t: return 'mankin'
    return 'silva'

def _student_result(s):
    first = (s.get('first_name') or '').strip()
    last = (s.get('last_name') or '').strip()
    res = {
        'found': True,
        'name': (last + ', ' + first).strip().strip(','),
        'first': first,
        'last': last,
        'period': str(s.get('period', '')),
        'course': s.get('course') or s.get('course_code') or '',
        'teacher': (s.get('instructor') or s.get('teacher_name') or 'Mr. Silva'),
        'student_cell': s.get('Student Cell') or s.get('student_cell') or '',
        'parent_guardian': s.get('Parent Guardian') or s.get('parent_guardian') or '',
        'parent_cell': s.get('Parent Cell') or s.get('parent_cell') or '',
    }
    # A saved contact override (student self-edit or teacher edit) wins over the base roster value.
    ov = _ROSTER_OVERRIDES.get(str(s.get('student_id', '')).strip())
    if ov:
        for k in ('student_cell', 'parent_guardian', 'parent_cell'):
            if ov.get(k) not in (None, ''):
                res[k] = ov[k]
    return res

DEMO_TEACHER_ID = 'teacher'   # Mr. Silva's self-demo account. Typing "teacher" as the student ID
                              # resolves to a labeled demo student (Period 1, Silva pool, always
                              # eligible) so he can walk a class through reserving a camera.

def _demo_teacher_result():
    """Synthetic student for the "teacher" demo ID. Clearly labeled (Demo) everywhere it shows, with
    fictitious 555 contacts so the reserve flow runs end to end without stalling on contact entry."""
    return {
        'found': True, 'name': 'Silva, Mr. (Demo)', 'first': 'Mr.', 'last': 'Silva',
        'period': '1', 'course': 'Photography 1 (Demo)', 'teacher': 'Mr. Silva',
        'student_cell': '(805) 555-0100', 'parent_guardian': 'Demo Parent',
        'parent_cell': '(805) 555-0101', 'demo': True,
    }

def resolve_student(student_id):
    """Look a student up for CAMERA CHECKOUT. Resolves photography students only (Silva Photo +
    Garcia Photo); Silva's Digital Arts students are intentionally not in the camera system.
    Never exposed on public endpoints; used server-side only."""
    sid = str(student_id).strip()
    if sid.lower() == DEMO_TEACHER_ID:
        return _demo_teacher_result()
    for s in _camera_students():
        if str(s.get('student_id', '')).strip() == sid:
            return _student_result(s)
    return {'found': False, 'name': '', 'first': '', 'last': '', 'period': '', 'course': '',
            'student_cell': '', 'parent_guardian': '', 'parent_cell': ''}

def _photowalk_seatmap():
    """For the Photo Walk Log: {period: {'course':..., 'seats': {seatNo: {'id':..., 'label':...}}}}.
    Built from Mr. Silva's roster (period + Seat + student_id), covering ALL his classes including
    Digital Arts (resolve_student is photo-only, so build names here). Authenticated use only."""
    out = {}
    for s in _roster_students():
        if not isinstance(s, dict):
            continue
        period = str(s.get('period', '')).strip().lstrip('0')
        seat = str(s.get('Seat', '')).strip().lstrip('0')
        sid = str(s.get('student_id', '')).strip()
        if not period or not seat or not sid:
            continue
        first = (s.get('preferred_name') or s.get('call_name') or s.get('first_name') or '').strip()
        last = (s.get('last_name') or '').strip()
        label = (first + ' ' + (last[:1] + '.' if last else '')).strip()
        rec = out.setdefault(period, {'course': s.get('course') or s.get('course_code') or '', 'seats': {}})
        rec['seats'][seat] = {'id': sid, 'label': label}
    return out

# ---------------------------------------------------------------------------
# Camera-reservation eligibility: block a Silva PHOTO student who has MORE THAN
# MISSING_LIMIT missing assignments in Silva's Photo courses. The check is LIVE
# against Canvas at ID entry and FAILS OPEN: any error/undetermined result => allowed,
# so a Canvas hiccup or an unmapped ID never blocks a legitimate student. Garcia's
# students are exempt (not in Silva's roster, and never in Silva's Canvas courses).
# ---------------------------------------------------------------------------
_silva_ids_cache = {'ids': None, 'exp': 0}
_photo_courses_cache = {'ids': None, 'exp': 0}
_canvas_cache = {}            # Canvas GET proxy cache: path -> (expiry, status, content_type, link, body)
_CANVAS_CACHE_TTL = 300       # 5 min. Speeds the dashboard's repeat course pulls; the Refresh button
_canvas_cache_lock = threading.Lock()   # sends X-Canvas-Fresh:1 to bypass the cache and refill it.
_elig_pass_cache = {}   # sid -> (expiry, result); caches only a DEFINITE pass, so a blocked
                        # student who turns work in is re-checked live on their next try.

def _silva_photo_ids():
    """Set of student_id strings for Mr. Silva's PHOTO students (roster_data.json).
    Only these students are subject to the missing-assignment gate. Cached briefly."""
    now = time.time()
    if _silva_ids_cache['ids'] is not None and now < _silva_ids_cache['exp']:
        return _silva_ids_cache['ids']
    ids = set()
    for s in _roster_students():
        if _is_photo(s):
            sid = str(s.get('student_id', '')).strip()
            if sid:
                ids.add(sid)
    _silva_ids_cache['ids'] = ids
    _silva_ids_cache['exp'] = now + 300   # 5 min; the roster rarely changes mid-session
    return ids

def _canvas_get_json(path, timeout=8):
    """Server-side GET to Canvas with the shared token. Returns (status, parsed_json, link)
    or (None, None, None) on failure. Never raises (the gate must fail open)."""
    if not CANVAS_TOKEN:
        return (None, None, None)
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(CANVAS_HOST, timeout=timeout, context=ctx)
    headers = {'Authorization': f'Bearer {CANVAS_TOKEN}', 'User-Agent': 'PVHS-Dashboard/2.0'}
    try:
        conn.request('GET', path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status >= 400:
            print(f'[eligibility] canvas GET {path} -> {resp.status}: {raw[:200]}')
            return (resp.status, None, None)
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        return (resp.status, parsed, resp.getheader('Link', ''))
    except Exception as e:
        print(f'[eligibility] canvas GET error {path}: {e}')
        return (None, None, None)
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _silva_photo_course_ids():
    """Silva's available PHOTO course IDs (the Canvas token is Silva's). Cached 1h.
    Returns a list of course-id strings, or [] if none/undetermined."""
    now = time.time()
    if _photo_courses_cache['ids'] is not None and now < _photo_courses_cache['exp']:
        return _photo_courses_cache['ids']
    _status, courses, _link = _canvas_get_json(
        '/api/v1/courses?enrollment_type=teacher&state[]=available&per_page=100')
    if isinstance(courses, list):
        ids = []
        for c in courses:
            name = ((c.get('name') or '') + ' ' + (c.get('course_code') or '')).lower()
            if 'photo' in name and c.get('id') is not None:
                ids.append(str(c['id']))
        _photo_courses_cache['ids'] = ids            # cache only a definite answer
        _photo_courses_cache['exp'] = now + 3600
        print(f'[eligibility] Silva photo course ids: {ids}')
        return ids
    return []   # API failure: leave cache empty so we retry next time

def _missing_count(sid):
    """Count a student's MISSING assignments across Silva's Photo courses via Canvas.
    Returns an int, or None if it could not be determined (=> fail open upstream)."""
    course_ids = _silva_photo_course_ids()
    if not course_ids:
        return None
    total, saw_ok = 0, False
    for cid in course_ids:
        # Teacher-scoped submissions for this ONE student; Canvas's 'missing' flag mirrors
        # exactly what the student sees as missing in that course.
        path = (f'/api/v1/courses/{cid}/students/submissions'
                f'?student_ids[]=sis_user_id:{sid}&per_page=100')
        _status, subs, _link = _canvas_get_json(path)
        if isinstance(subs, list):
            saw_ok = True
            for s in subs:
                if s.get('missing') is True:
                    total += 1
    return total if saw_ok else None

def camera_eligibility(sid):
    """Eligibility for the camera-reservation gate. Silva PHOTO students only; fail-open.
    Returns {'gated':bool, 'eligible':bool, 'missing':int|None, 'undetermined':bool}."""
    sid = str(sid).strip()
    if sid not in _silva_photo_ids():
        return {'gated': False, 'eligible': True, 'missing': None, 'undetermined': False}
    now = time.time()
    cached = _elig_pass_cache.get(sid)
    if cached and now < cached[0]:
        return cached[1]   # a recent clean pass (covers ID-entry -> checkout without re-hitting Canvas)
    count = _missing_count(sid)
    if count is None:
        return {'gated': True, 'eligible': True, 'missing': None, 'undetermined': True}
    eligible = (count <= MISSING_LIMIT)
    res = {'gated': True, 'eligible': eligible, 'missing': count, 'undetermined': False}
    if eligible:
        _elig_pass_cache[sid] = (now + 300, res)   # cache passes only; blocked students re-check live
    print(f'[eligibility] sid={sid} missing={count} limit={MISSING_LIMIT} eligible={eligible}')
    return res

# ---------------------------------------------------------------------------
# Firebase ID-token verification (lightweight, no Admin SDK needed)
# ---------------------------------------------------------------------------

_cert_cache = {'data': None, 'exp': 0}

def _fetch_google_certs():
    """Fetch Google's public certs for Firebase token verification, cached."""
    import urllib.request as req
    now = time.time()
    if _cert_cache['data'] and now < _cert_cache['exp']:
        return _cert_cache['data']
    url = ('https://www.googleapis.com/robot/v1/metadata/x509/'
           'securetoken@system.gserviceaccount.com')
    with req.urlopen(req.Request(url), timeout=10) as r:
        _cert_cache['data'] = json.loads(r.read())
        _cert_cache['exp'] = now + 3600
    return _cert_cache['data']


def verify_firebase_token(id_token_str):
    """Verify a Firebase ID token. Returns decoded claims or None."""
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as g_requests
        claims = id_token.verify_firebase_token(
            id_token_str,
            g_requests.Request(),
            audience=FIREBASE_PROJECT_ID,
        )
        return claims
    except Exception as e:
        print(f'[auth] Token verification failed: {e}')
        return None


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    # -- Auth helpers -------------------------------------------------------

    def check_auth(self):
        """Verify the request is authenticated. Returns email or None."""
        # API-key auth (server-to-server, e.g. curriculum catalog)
        key = self.headers.get('X-API-Key', '')
        if API_KEY and key == API_KEY:
            return 'api-key'

        # Firebase ID token auth (browser)
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return None
        token = auth[7:]
        claims = verify_firebase_token(token)
        if not claims:
            return None
        email = (claims.get('email') or '').lower()
        if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
            print(f'[auth] Email not allowed: {email}')
            return None
        return email

    def require_auth(self):
        """Check auth; send 401 and return False if unauthenticated."""
        email = self.check_auth()
        if not email:
            self.json_response(401, {'error': 'Unauthorized'})
            return False
        return True

    def check_camera_auth(self):
        """Camera-scoped auth: a full login (Firebase/API key) OR the shared
        camera password. Used only by the camera checkout endpoints."""
        if self.check_auth():
            return True
        if CAMERA_PIN and self.headers.get('X-Camera-Key', '') == CAMERA_PIN:
            return True
        return False

    def require_camera_auth(self):
        if not self.check_camera_auth():
            self.json_response(401, {'error': 'Unauthorized'})
            return False
        return True

    # -- Routing ------------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith('/api/v1/'):
            if not self.require_auth():
                return
            self.proxy_canvas('GET')
        elif path == '/api/status':
            self.handle_status()
        elif path == '/api/roster':
            if not self.require_auth():
                return
            self.handle_roster()
        elif path == '/api/camera/calendar':
            self.handle_camera_calendar()          # PUBLIC, sanitized (no names/phones/IDs)
        elif path == '/api/camera/cameras':
            self.handle_camera_cameras()           # PUBLIC, inventory list only
        elif path == '/api/camera/checkouts':
            if not self.require_camera_auth():
                return
            self.handle_camera_checkouts()         # camera-scoped: full detail for the teacher
        elif path == '/api/camera/assets':
            if not self.require_camera_auth():
                return
            self.handle_camera_assets()            # per-camera standing notes + out-of-service
        elif path in ('/', ''):
            self.path = '/index.html'
            self.serve_file()
        else:
            self.serve_file()

    def do_POST(self):
        path = urlparse(self.path).path
        # PUBLIC student checkout (no auth: the static site can't hold a secret key).
        # Validated server-side; resolves the ID to a name without echoing it back.
        if path == '/api/camera/checkout':
            self.handle_camera_checkout()
            return
        if path == '/api/camera/lookup':
            self.handle_camera_lookup()        # PUBLIC: confirm full ID -> "First L." only
            return
        if path == '/api/camera/student_info':
            self.handle_camera_student_info()  # PUBLIC: full contact for a full ID, for the self-edit form
            return
        if path == '/api/camera/student_update':
            self.handle_camera_student_update()# PUBLIC: a student corrects their own contact -> overrides store
            return
        # Camera management: full login OR the shared camera password (scoped to cameras only)
        if path in ('/api/camera/return', '/api/camera/status', '/api/camera/update', '/api/camera/delete', '/api/camera/asset', '/api/camera/garcia_roster', '/api/camera/mankin_roster', '/api/camera/card_hold', '/api/camera/card_return', '/api/camera/student_override', '/api/camera/blackout', '/api/camera/seatmap', '/api/camera/photowalk'):
            if not self.require_camera_auth():
                return
            if path == '/api/camera/seatmap':  self.handle_camera_seatmap();  return
            if path == '/api/camera/photowalk': self.handle_camera_photowalk(); return
            if path == '/api/camera/return':   self.handle_camera_return()   # mark returned
            elif path == '/api/camera/status': self.handle_camera_status()   # reserved -> out -> returned
            elif path == '/api/camera/update': self.handle_camera_update()   # edit a checkout
            elif path == '/api/camera/delete': self.handle_camera_delete()   # remove a checkout
            elif path == '/api/camera/asset':  self.handle_camera_asset()    # per-camera standing note / out-of-service
            elif path == '/api/camera/garcia_roster': self.handle_camera_garcia_roster()  # load Garcia's camera-only roster
            elif path == '/api/camera/mankin_roster': self.handle_camera_mankin_roster()  # load Mankin's camera roster
            elif path == '/api/camera/card_hold':   self.handle_camera_card_hold()    # student left card -> wallet slot + flag camera
            elif path == '/api/camera/card_return': self.handle_camera_card_return()  # card returned to student -> free slot
            elif path == '/api/camera/student_override': self.handle_camera_student_override()  # teacher edits a student's contact -> profile
            elif path == '/api/camera/blackout': self.handle_camera_blackout()  # teacher closes/opens a day to checkouts
            return
        if not self.require_auth():
            return
        if path == '/api/auth/verify':
            self.handle_auth_verify()
        elif path == '/api/roster/upload':
            self.handle_roster_upload()
        elif path == '/api/batch/grades':
            self.handle_batch_grades()
        elif path == '/api/batch/comments':
            self.handle_bulk_comments()
        elif path.startswith('/api/v1/'):
            self.proxy_canvas('POST')
        else:
            self.send_error(404)

    def do_PUT(self):
        if not self.require_auth():
            return
        if urlparse(self.path).path.startswith('/api/v1/'):
            self.proxy_canvas('PUT')
        else:
            self.send_error(404)

    def do_DELETE(self):
        if not self.require_auth():
            return
        if urlparse(self.path).path.startswith('/api/v1/'):
            self.proxy_canvas('DELETE')
        else:
            self.send_error(404)

    # -- Auth verify --------------------------------------------------------

    def handle_auth_verify(self):
        """Return user info after successful auth check."""
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            self.json_response(401, {'error': 'No token'})
            return
        claims = verify_firebase_token(auth[7:])
        if not claims:
            self.json_response(401, {'error': 'Invalid token'})
            return
        email = (claims.get('email') or '').lower()
        if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
            self.json_response(403, {'error': 'Email not authorized'})
            return
        self.json_response(200, {
            'email': email,
            'name': claims.get('name', ''),
            'picture': claims.get('picture', ''),
            'uid': claims.get('sub', ''),
            'canvas_connected': bool(CANVAS_TOKEN),
        })

    # -- Status (unauthenticated) -------------------------------------------

    def handle_status(self):
        self.json_response(200, {
            'ok': True,
            'canvas_host': CANVAS_HOST,
            'canvas_connected': bool(CANVAS_TOKEN),
        })

    # -- Roster data (authenticated) ----------------------------------------

    def handle_roster(self):
        roster_path = os.path.join(SERVE_DIR, 'roster_data.json')
        if not os.path.exists(roster_path):
            self.json_response(404, {'error': 'No roster data uploaded'})
            return
        with open(roster_path, 'r') as f:
            data = json.load(f)
        self.json_response(200, data)

    def handle_roster_upload(self):
        length = int(self.headers.get('Content-Length', 0))
        if length > 5_000_000:
            self.json_response(413, {'error': 'Roster data too large'})
            return
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.json_response(400, {'error': 'Invalid JSON'})
            return
        if 'students' not in data or 'sections' not in data:
            self.json_response(400, {'error': 'Missing students or sections'})
            return
        roster_path = os.path.join(SERVE_DIR, 'roster_data.json')
        with open(roster_path, 'w') as f:
            json.dump(data, f)
        print(f'[roster] Uploaded: {len(data["students"])} students, {len(data["sections"])} sections')
        self.json_response(200, {
            'ok': True,
            'students': len(data['students']),
            'sections': len(data['sections']),
        })

    # -- Camera checkout ----------------------------------------------------

    def _client_ip(self):
        xff = self.headers.get('X-Forwarded-For', '')
        return xff.split(',')[0].strip() if xff else self.client_address[0]

    def handle_camera_cameras(self):
        """PUBLIC: the camera inventory only (for the form dropdown + the calendar)."""
        self.json_response(200, {'cameras': CAMERAS})

    def handle_camera_calendar(self):
        """PUBLIC: sanitized CAMERA checkouts. NO names, IDs, or phone numbers, ever.
        Equipment records are excluded (the public calendar tracks cameras only)."""
        out = []
        for c in load_checkouts():
            if c.get('kind') == 'equipment':
                continue
            st = status_of(c)
            out.append({
                'id': c.get('id'),
                'camera': c.get('camera', ''),
                'out': c.get('out', ''),
                'due': c.get('due', ''),
                'picked_up_date': c.get('picked_up_date', ''),
                'returned': (st == 'returned'),
                'returned_date': c.get('returned_date', ''),
                'status': st,
            })
        # Out-of-service and no-card cameras (names only, no PII) so the public reserve flow can block them.
        assets = load_assets()
        oos = [cam for cam in ALL_CAMERAS if (assets.get(cam) or {}).get('oos')]
        no_card = [cam for cam in ALL_CAMERAS if (assets.get(cam) or {}).get('no_card')]
        # Kit contents: missing-item list per camera (no PII) so students see if a kit is incomplete.
        kits = {cam: (assets.get(cam) or {}).get('kit', []) for cam in ALL_CAMERAS if (assets.get(cam) or {}).get('kit')}
        # Equipment (gear) reservations, SANITIZED (unit id + dates + status only, never a name/ID/phone)
        # so the per-teacher "Other Equipment" tab can compute per-UNIT availability, exactly like cameras.
        equip = []
        for c in load_checkouts():
            if c.get('kind') != 'equipment':
                continue
            est = status_of(c)
            equip.append({'id': c.get('id'), 'item': c.get('item', ''), 'out': c.get('out', ''),
                          'due': c.get('due', ''), 'returned': (est == 'returned'),
                          'returned_date': c.get('returned_date', ''), 'status': est})
        # Per-unit gear out-of-service + kit (missing-item) lists, mirroring the camera oos/kits above.
        equip_oos = [u for u in EQUIPMENT_UNIT_IDS if (assets.get(u) or {}).get('oos')]
        equip_kits = {u: (assets.get(u) or {}).get('kit', []) for u in EQUIPMENT_UNIT_IDS if (assets.get(u) or {}).get('kit')}
        # `cameras` stays 01-18 for backward compatibility; `pools` drives the new per-teacher views.
        self.json_response(200, {'cameras': CAMERAS, 'checkouts': out, 'oos': oos, 'no_card': no_card,
                                 'kits': kits, 'blackouts': _blackout_list(),
                                 'pools': CAMERA_POOLS, 'equipment_pools': EQUIPMENT_POOLS,
                                 'equipment_units': EQUIPMENT_UNITS, 'equipment_types': EQUIPMENT_TYPE_LABELS,
                                 'equipment': equip, 'equipment_oos': equip_oos, 'equipment_kits': equip_kits})

    def handle_camera_checkouts(self):
        """Camera-scoped: full detail for the teacher (names + emergency phones).
        Strips the internal _ip field before sending. Includes the per-camera asset store
        and the memory-card wallet."""
        items = [{k: v for k, v in c.items() if k != '_ip'} for c in load_checkouts()]
        # Enrich each held card with the student's contact + period + teacher (authed feed only) so
        # the console can show who to reach out to about an un-offloaded card.
        cards = {}
        for slot, c in (load_memcards() or {}).items():
            c = dict(c)
            sid = str(c.get('student_id', '')).strip()
            if sid:
                r = resolve_student(sid)
                if r.get('found'):
                    if not c.get('student_name'):
                        c['student_name'] = r.get('name', '')
                    c['student_cell'] = r.get('student_cell', '')
                    c['parent_guardian'] = r.get('parent_guardian', '')
                    c['parent_cell'] = r.get('parent_cell', '')
                    c['period'] = r.get('period', '')
                    c['course'] = r.get('course', '')
                    c['teacher'] = r.get('teacher', '')
            cards[slot] = c
        # Teacher console gets ALL cameras (01-21) so the Silva/Garcia/Mankin view toggle can show each
        # pool; the console filters to the selected pool with DATA.pools. (Public feed stays 01-18.)
        self.json_response(200, {'cameras': ALL_CAMERAS, 'checkouts': items,
                                 'assets': load_assets(), 'cards': cards,
                                 'blackouts': _blackout_list(),
                                 'missing_limit': MISSING_LIMIT,
                                 'pools': CAMERA_POOLS, 'equipment_pools': EQUIPMENT_POOLS,
                                 'equipment_units': EQUIPMENT_UNITS, 'equipment_types': EQUIPMENT_TYPE_LABELS})

    def handle_camera_assets(self):
        """Camera-scoped: the per-camera standing notes + out-of-service flags."""
        self.json_response(200, {'assets': load_assets()})

    def handle_camera_garcia_roster(self):
        """Camera-scoped: replace Garcia's camera-only roster (persisted to disk). Body:
        {students:[...]}. These are used ONLY for camera resolution, never the dashboard roster."""
        data = self._read_json_body()
        if data is None:
            return
        students = data.get('students') if isinstance(data, dict) else (data if isinstance(data, list) else None)
        if not isinstance(students, list) or not students:
            self.json_response(400, {'error': 'Provide a non-empty students list.'})
            return
        with _garcia_lock:
            os.makedirs(_data_dir(), exist_ok=True)
            with open(GARCIA_PATH, 'w') as f:
                json.dump({'students': students,
                           'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}, f)
            _load_garcia()
        self.json_response(200, {'ok': True, 'count': len(_GARCIA_STUDENTS)})

    def handle_camera_mankin_roster(self):
        """Camera-scoped: replace Ms. Mankin's Digital Arts camera roster (persisted to disk). Body:
        {students:[...]}. Used ONLY for camera resolution, never the dashboard roster."""
        data = self._read_json_body()
        if data is None:
            return
        students = data.get('students') if isinstance(data, dict) else (data if isinstance(data, list) else None)
        if not isinstance(students, list) or not students:
            self.json_response(400, {'error': 'Provide a non-empty students list.'})
            return
        with _mankin_lock:
            os.makedirs(_data_dir(), exist_ok=True)
            with open(MANKIN_PATH, 'w') as f:
                json.dump({'students': students,
                           'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}, f)
            _load_mankin()
        self.json_response(200, {'ok': True, 'count': len(_MANKIN_STUDENTS)})

    def handle_camera_blackout(self):
        """Camera-scoped: the teacher closes/opens a day to student checkouts (an absence, etc.).
        Body {date:'YYYY-MM-DD', on:true|false}. No reason is stored or shown to students."""
        data = self._read_json_body()
        if data is None:
            return
        date = str(data.get('date', '')).strip()
        p = date.split('-')
        if len(date) != 10 or len(p) != 3 or not (p[0].isdigit() and p[1].isdigit() and p[2].isdigit()):
            self.json_response(400, {'error': 'Provide a date as YYYY-MM-DD.'})
            return
        on = bool(data.get('on', True))
        with _blackouts_lock:
            b = _load_blackouts()
            if on:
                b[date] = {'added': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
            else:
                b.pop(date, None)
            _save_blackouts(b)
        self.json_response(200, {'ok': True, 'blackouts': sorted(_load_blackouts().keys())})

    def handle_camera_asset(self):
        """Camera-scoped: set a camera's (or gear unit's) standing note, out-of-service flag, and kit.
        Body: {camera, note?, oos?, kit?, issues?, log?}. The note follows the physical unit across
        checkouts. `camera` may also be a per-unit gear id (e.g. tripod-03, reflector-01). A gear unit
        behaves like a camera (note/oos/kit/issues/log); only `no_card` is camera-only. The public feed
        keeps cameras and gear separate by filtering on ALL_CAMERAS vs EQUIPMENT_UNITS, so a gear unit's
        oos/kit never leaks into the camera lists."""
        data = self._read_json_body()
        if data is None:
            return
        cam = str(data.get('camera', '')).strip()
        is_gear_unit = cam in EQUIPMENT_UNITS
        if cam not in CAMERAS and not is_gear_unit:
            self.json_response(400, {'error': 'Unknown camera or item'})
            return
        with _assets_lock:
            assets = load_assets()
            rec = assets.get(cam) or {'note': '', 'oos': False, 'issues': [], 'log': []}
            if 'note' in data:
                rec['note'] = str(data.get('note', ''))
            # issues = current open condition items (removable); log = permanent dated history.
            if 'issues' in data and isinstance(data['issues'], list):
                rec['issues'] = [str(x) for x in data['issues']]
            if 'log' in data and isinstance(data['log'], list):
                rec['log'] = [{'date': str(e.get('date', '')), 'text': str(e.get('text', ''))}
                              for e in data['log'] if isinstance(e, dict)]
            # Cameras and gear units share oos + kit (list of MISSING item labels).
            if 'oos' in data:
                rec['oos'] = bool(data.get('oos'))
            if 'kit' in data and isinstance(data['kit'], list):
                rec['kit'] = [str(x) for x in data['kit']][:40]
            if not is_gear_unit:
                if 'no_card' in data:             # memory card missing until a fresh one is installed
                    rec['no_card'] = bool(data.get('no_card'))
            rec.setdefault('issues', [])
            rec.setdefault('log', [])
            rec['updated'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            assets[cam] = rec
            save_assets(assets)
        self.json_response(200, {'ok': True, 'camera': cam, 'asset': assets[cam]})

    def handle_camera_card_hold(self):
        """Camera-scoped: a student left their memory card to offload later. Drop it into a wallet
        slot (1-18) with a note, and flag the source camera 'no card' so it does not go back out
        until a fresh card is installed. Body: {slot, from_camera, student_id?, student_name?,
        note?, checkout_id?}."""
        data = self._read_json_body()
        if data is None:
            return
        try:
            slot = int(str(data.get('slot', '')).strip())
        except ValueError:
            slot = 0
        if slot < 1 or slot > CARD_SLOTS:
            self.json_response(400, {'error': 'Pick a wallet slot 1-%d.' % CARD_SLOTS})
            return
        cam = str(data.get('from_camera', '')).strip()
        sid = str(data.get('student_id', '')).strip()
        sname = str(data.get('student_name', '')).strip()
        if sid and not sname:                            # ad-hoc hold: resolve the name from the ID
            r = resolve_student(sid)
            if r.get('found'):
                sname = r.get('name', '')
        rec = {
            'slot': slot,
            'student_id': sid,
            'student_name': sname,
            'from_camera': cam,
            'note': str(data.get('note', '')).strip(),
            'checkout_id': str(data.get('checkout_id', '')).strip(),
            'held_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }
        with _memcards_lock:
            cards = load_memcards()
            if cards.get(str(slot)):                     # never silently overwrite a card already in this slot
                self.json_response(409, {'error': 'Slot %d already holds a card. Pick an empty slot or free it first.' % slot})
                return
            cards[str(slot)] = rec
            save_memcards(cards)
        # Flag the camera as missing its card (kit incomplete) until a fresh card is installed.
        if cam in CAMERAS:
            with _assets_lock:
                assets = load_assets()
                a = assets.get(cam) or {'note': '', 'oos': False, 'issues': [], 'log': []}
                a['no_card'] = True
                a.setdefault('issues', [])
                a.setdefault('log', [])
                a['updated'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                assets[cam] = a
                save_assets(assets)
        self.json_response(200, {'ok': True, 'cards': load_memcards()})

    def handle_camera_card_return(self):
        """Camera-scoped: the student came back, offloaded, and we returned their card, so the
        wallet slot is freed and the card info goes away. Body: {slot}."""
        data = self._read_json_body()
        if data is None:
            return
        slot = str(data.get('slot', '')).strip()
        with _memcards_lock:
            cards = load_memcards()
            cards.pop(slot, None)
            save_memcards(cards)
        self.json_response(200, {'ok': True, 'cards': load_memcards()})

    def handle_camera_lookup(self):
        """PUBLIC: confirm a FULL student ID resolves, returning ONLY a first name +
        last initial so a student can verify themselves before reserving. There is no
        prefix search, so the public page cannot be used to browse the roster, and no
        ID, phone, or full last name is ever echoed. Rate limited per IP."""
        if not _rate_ok(self._client_ip()):
            self.json_response(429, {'found': False, 'error': 'Too many tries, slow down.'})
            return
        data = self._read_json_body()
        if data is None:
            return
        sid = ''.join(ch for ch in str(data.get('student_id', '')) if ch.isdigit())
        if len(sid) < 5:                    # require a full-length ID; no short prefixes
            self.json_response(200, {'found': False})
            return
        r = resolve_student(sid)
        if not r['found']:
            self.json_response(200, {'found': False})
            return
        first = (r.get('first') or '').strip()
        last = (r.get('last') or '').strip()
        label = (first + ' ' + (last[:1] + '.' if last else '')).strip()
        def last4(s):
            d = ''.join(ch for ch in str(s or '') if ch.isdigit())
            return d[-4:] if len(d) >= 4 else ''
        # Masked verification only: a first name + last initial, the class period (for Photo-2 gating),
        # and the LAST 4 digits of the phones on file so a student can confirm "yes that's my number"
        # or see it is missing. No full phone, no full last name, no parent name is ever echoed.
        scl = last4(r.get('student_cell')); pcl = last4(r.get('parent_cell'))
        self.json_response(200, {
            'found': True, 'label': label, 'period': str(r.get('period', '')),
            'student_last4': scl, 'parent_last4': pcl,
            'has_student_cell': bool(scl), 'has_parent_cell': bool(pcl),
        })

    def handle_camera_student_info(self):
        """PUBLIC: return the FULL contact on file for a full student ID, so a student can
        review and correct their own info on the reserve page. Requires a complete ID (no
        prefix browsing) and is rate limited. (Teacher's decision: students see and fix their
        own info; a full ID is required to see anything.)"""
        if not _rate_ok(self._client_ip()):
            self.json_response(429, {'found': False, 'error': 'Too many tries, slow down.'})
            return
        data = self._read_json_body()
        if data is None:
            return
        raw = str(data.get('student_id', '')).strip()
        if raw.lower() == DEMO_TEACHER_ID:
            sid = DEMO_TEACHER_ID   # Mr. Silva's demo account: skip the digits-only requirement.
        else:
            sid = ''.join(ch for ch in raw if ch.isdigit())
            if len(sid) < 5:
                self.json_response(200, {'found': False})
                return
        r = resolve_student(sid)
        if not r['found']:
            self.json_response(200, {'found': False})
            return
        first = (r.get('first') or '').strip()
        last = (r.get('last') or '').strip()
        label = (first + ' ' + (last[:1] + '.' if last else '')).strip()
        # Missing-assignment gate (Silva Photo students only; live Canvas; fails open).
        # A blocked student gets NO contact info back: they never reach the review screen.
        elig = camera_eligibility(sid)
        if not elig['eligible']:
            self.json_response(200, {
                'found': True, 'eligible': False, 'label': label,
                'missing_count': elig.get('missing'), 'missing_limit': MISSING_LIMIT,
            })
            return
        self.json_response(200, {
            'found': True,
            'eligible': True,
            'label': label,
            'name': r.get('name', ''),
            'period': str(r.get('period', '')),
            'teacher': r.get('teacher', ''),
            'course': r.get('course', ''),
            'pool': _pool_for(r.get('teacher', '')),
            'student_cell': r.get('student_cell', ''),
            'parent_guardian': r.get('parent_guardian', ''),
            'parent_cell': r.get('parent_cell', ''),
        })

    def handle_camera_student_update(self):
        """PUBLIC: a student corrects their OWN contact info. Writes to the persistent overrides
        store (never the base roster), so it survives redeploys and is reversible. Only contact
        fields are accepted, the ID must resolve to a real roster student, and it is rate limited
        with the client IP recorded for traceability."""
        if not _rate_ok(self._client_ip()):
            self.json_response(429, {'ok': False, 'error': 'Too many tries, slow down.'})
            return
        data = self._read_json_body()
        if data is None:
            return
        sid = ''.join(ch for ch in str(data.get('student_id', '')) if ch.isdigit())
        if len(sid) < 5 or not resolve_student(sid)['found']:
            self.json_response(200, {'ok': False, 'error': 'We could not find that ID.'})
            return
        if not _write_override(sid, data, self._client_ip()):
            self.json_response(200, {'ok': False, 'error': 'Nothing to update.'})
            return
        r = resolve_student(sid)
        self.json_response(200, {
            'ok': True,
            'student_cell': r.get('student_cell', ''),
            'parent_guardian': r.get('parent_guardian', ''),
            'parent_cell': r.get('parent_cell', ''),
        })

    def handle_camera_student_override(self):
        """CAMERA-SCOPED (teacher): correct a student's contact -> persistent overrides store, so the
        fix sticks on the student's PROFILE for FUTURE checkouts. Trusted caller, no rate limit."""
        data = self._read_json_body()
        if data is None:
            return
        sid = ''.join(ch for ch in str(data.get('student_id', '')) if ch.isdigit())
        if len(sid) < 5 or not resolve_student(sid)['found']:
            self.json_response(200, {'ok': False, 'error': 'We could not find that ID.'})
            return
        _write_override(sid, data, self._client_ip())
        r = resolve_student(sid)
        self.json_response(200, {
            'ok': True,
            'student_cell': r.get('student_cell', ''),
            'parent_guardian': r.get('parent_guardian', ''),
            'parent_cell': r.get('parent_cell', ''),
        })

    def _new_checkout_rec(self, data, kind, item, camera, out, due, group=''):
        """Build one checkout record (camera OR equipment), resolving the student server-side."""
        sid = str(data.get('student_id', '')).strip()
        r = resolve_student(sid)
        flags = []
        if not r['found']:
            flags.append('unknown student id')
        if kind == 'camera' and camera not in CAMERAS:
            flags.append('unknown camera')
        if kind == 'equipment' and item not in EQUIPMENT_UNITS:
            flags.append('unknown item')
        rec = {
            'id': 'ck_' + str(int(time.time() * 1000)) + ('' if not group else '_' + item),
            'kind': kind,                      # 'camera' or 'equipment'
            'item': item,                      # equipment key (tripod/wide/zoom/speedlite); '' for cameras
            'group': str(group or ''),         # links an accessory to its camera checkout id
            'student_id': sid,
            'student_name': r['name'],
            'period': r['period'],
            'course': r['course'],
            'student_cell': r['student_cell'],
            'parent_guardian': r['parent_guardian'],
            'parent_cell': r['parent_cell'],
            'camera': camera,
            'out': out,
            'due': due,
            'status': 'reserved',
            'picked_up_date': '',
            'returned': False,
            'returned_date': '',
            'note': str(data.get('note', '')),  # condition note (missing cap, lost plate, etc.)
            # Student self-reservations arrive with pending=true and need teacher approval; teacher-made
            # reservations (from the console) are approved on creation.
            'approved': (False if data.get('pending') else True),
            'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'flag': '; '.join(flags),
            '_ip': self._client_ip(),
        }
        stamp_status(rec, data.get('status'))
        return rec

    def handle_camera_seatmap(self):
        """AUTHED (camera key). Seat -> student per period for the Photo Walk Log. Server-side only."""
        self.json_response(200, {'periods': _photowalk_seatmap()})

    def handle_camera_photowalk(self):
        """AUTHED (camera key). Photo Walk Log store: action = save | list | get.
        One log per date+period (key "YYYY-MM-DD|<period>"); saving the same day overwrites it."""
        data = self._read_json_body()
        if data is None:
            return
        action = str(data.get('action', '')).strip()
        if action == 'list':
            d = load_photowalks()
            rows = [{'key': k, 'date': v.get('date'), 'period': v.get('period'),
                     'course': v.get('course'), 'saved_at': v.get('saved_at'),
                     'kits': len(v.get('kits', []))} for k, v in d.items() if isinstance(v, dict)]
            rows.sort(key=lambda r: (str(r.get('date', '')), str(r.get('period', ''))), reverse=True)
            self.json_response(200, {'logs': rows})
            return
        if action == 'get':
            key = str(data.get('key', '')).strip() or (str(data.get('date', '')).strip() + '|' + str(data.get('period', '')).strip())
            self.json_response(200, {'log': load_photowalks().get(key)})
            return
        if action == 'save':
            date = str(data.get('date', '')).strip()
            period = str(data.get('period', '')).strip()
            if not date or not period:
                self.json_response(400, {'error': 'Missing date or period.'})
                return
            rec = {'date': date, 'period': period, 'course': str(data.get('course', '')),
                   'saved_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                   'kits': data.get('kits') if isinstance(data.get('kits'), list) else []}
            with _photowalk_lock:
                d = load_photowalks()
                d[date + '|' + period] = rec
                save_photowalks(d)
            self.json_response(200, {'ok': True, 'key': date + '|' + period})
            return
        if action == 'delete':
            key = str(data.get('key', '')).strip() or (str(data.get('date', '')).strip() + '|' + str(data.get('period', '')).strip())
            with _photowalk_lock:
                d = load_photowalks()
                if key in d:
                    del d[key]
                    save_photowalks(d)
            self.json_response(200, {'ok': True})
            return
        self.json_response(400, {'error': 'Unknown action.'})

    def handle_camera_checkout(self):
        """PUBLIC submit. Student enters ID + camera + dates (+ optional accessories). We resolve
        the ID to a name/phones server-side and store it, returning a generic ack with NO name
        echoed. Accessories in `extras` (list of item keys) become their OWN linked records so each
        checks in independently. Unknown IDs are recorded + flagged, not rejected."""
        data = self._read_json_body()
        if data is None:
            return
        # The teacher console sends the camera key and is trusted to override (rapid legit writes,
        # deliberate double-books). Public student submissions are rate limited + validated below.
        is_teacher = self.check_camera_auth()
        if not is_teacher and not _rate_ok(self._client_ip()):
            self.json_response(429, {'error': 'Too many tries, slow down.'})
            return
        sid = str(data.get('student_id', '')).strip()
        kind = (str(data.get('kind', 'camera')).strip().lower() or 'camera')
        due = str(data.get('due', '')).strip()
        out = str(data.get('out', '')).strip() or time.strftime('%Y-%m-%d')
        if kind == 'equipment':
            item = str(data.get('item', '')).strip()
            if not sid or not item or not due:
                self.json_response(400, {'error': 'Enter a student ID, an item, and a due date.'})
                return
            if item not in EQUIPMENT_UNITS:
                self.json_response(400, {'error': 'Unknown item.'})
                return
            if due < out:
                self.json_response(400, {'error': 'The due date is before the pickup date.'})
                return
            # Student-facing guards (the teacher console is trusted to override these). Defense in
            # depth: the client already blocks ineligible students and only shows free units, but a
            # direct or stale call must not slip through the gear path either. All fail open. Per-unit,
            # exactly like a camera: a unit is available if it is not out of service and not already
            # booked over the requested span.
            if not is_teacher:
                if not camera_eligibility(sid)['eligible']:
                    self.json_response(403, {'error': 'You are not eligible to reserve equipment right now because you have more than %d missing assignments. Please turn in your missing work and try again later.' % MISSING_LIMIT})
                    return
                if _is_blackout(out):
                    self.json_response(409, {'error': 'That day is not available for checkouts. Please pick another day.'})
                    return
                if (load_assets().get(item) or {}).get('oos'):
                    self.json_response(409, {'error': 'That unit is out of service right now. Please pick another.'})
                    return
                if self._equipment_unit_busy(item, out, due):
                    self.json_response(409, {'error': 'That unit is already booked for those days. Please pick another day or unit.'})
                    return
            rec = self._new_checkout_rec(data, 'equipment', item, '', out, due, str(data.get('group', '')))
            with _checkouts_lock:
                items = load_checkouts(); items.append(rec); save_checkouts(items)
            self.json_response(200, {'ok': True, 'id': rec['id'], 'message': 'Reserved.'})
            return
        camera = str(data.get('camera', '')).strip()
        if not sid or not camera or not due:
            self.json_response(400, {'error': 'Please enter your student ID, a camera, and a due date.'})
            return
        if due < out:
            self.json_response(400, {'error': 'The due date is before the pickup date.'})
            return
        # Student-facing guards (the teacher console is trusted to override these).
        if not is_teacher:
            # Defense in depth: the client blocks ineligible students at ID entry, but re-check
            # here so the gate can't be skipped by calling the API directly. Fails open.
            if not camera_eligibility(sid)['eligible']:
                self.json_response(403, {'error': 'You are not eligible to reserve a camera right now because you have more than %d missing assignments. Please turn in your missing work and try again later.' % MISSING_LIMIT})
                return
            # Teacher-closed (blackout) day: not available for student checkouts. No reason shown.
            if _is_blackout(out):
                self.json_response(409, {'error': 'That day is not available for checkouts. Please pick another day.'})
                return
            if camera not in ALL_CAMERAS:
                self.json_response(400, {'error': 'Unknown camera.'})
                return
            a = load_assets().get(camera) or {}
            if a.get('oos'):
                self.json_response(409, {'error': 'That camera is out of service right now. Please pick another.'})
                return
            if a.get('no_card'):
                self.json_response(409, {'error': 'That camera is missing its memory card right now. Please pick another.'})
                return
            if camera in ('Cam 16', 'Cam 17', 'Cam 18'):
                per = str(resolve_student(sid).get('period', '')).lstrip('0')
                if per != '4':
                    # Photo-1 student on a Photo-2 camera: allowed only when the teacher enters the
                    # override code on the student's device. Validated here; the code never leaves the server.
                    if str(data.get('override', '')).strip() != CAMERA_OVERRIDE_CODE:
                        self.json_response(403, {'error': 'That camera is for Photography 2. Ask Mr. Silva to enter the teacher code to unlock it for you.', 'override_required': True})
                        return
            if self._camera_busy(camera, out, due):
                self.json_response(409, {'error': 'That camera is already booked for those days. Please pick another.'})
                return
        rec = self._new_checkout_rec(data, 'camera', '', camera, out, due)
        extras = data.get('extras') or []
        extra_recs = []
        if isinstance(extras, list):
            for key in extras:
                key = str(key).strip()
                if key in EQUIPMENT_UNITS:
                    extra_recs.append(self._new_checkout_rec(data, 'equipment', key, '', out, due, rec['id']))
        with _checkouts_lock:
            items = load_checkouts()
            items.append(rec)
            items.extend(extra_recs)
            save_checkouts(items)
        self.json_response(200, {'ok': True, 'id': rec['id'], 'message': 'Reserved. See Mr. Silva to pick up your camera.'})

    def _camera_busy(self, camera, out, due):
        """True if a non-returned checkout for this camera overlaps [out, due) (half-open, so a
        same-day handoff where one returns on the day the next picks up is allowed)."""
        for c in load_checkouts():
            if c.get('kind') == 'equipment' or c.get('camera') != camera:
                continue
            if status_of(c) == 'returned':
                continue
            eo = str(c.get('out', '')); ed = str(c.get('due', '')) or eo
            if eo and ed and eo < due and out < ed:
                return True
        return False

    def _equipment_unit_busy(self, item, out, due):
        """True if this specific gear unit (item = unit id) has a non-returned reservation overlapping
        [out, due) (half-open, so a same-day handoff is allowed). Mirrors _camera_busy for per-unit gear."""
        for c in load_checkouts():
            if c.get('kind') != 'equipment' or c.get('item') != item:
                continue
            if status_of(c) == 'returned':
                continue
            eo = str(c.get('out', '')); ed = str(c.get('due', '')) or eo
            if eo and ed and eo < due and out < ed:
                return True
        return False

    def _find_checkout(self, items, cid):
        for x in items:
            if x.get('id') == cid:
                return x
        return None

    def handle_camera_return(self):
        data = self._read_json_body()
        if data is None:
            return
        cid = str(data.get('id', ''))
        with _checkouts_lock:
            items = load_checkouts()
            c = self._find_checkout(items, cid)
            if not c:
                self.json_response(404, {'error': 'Checkout not found'})
                return
            c['returned'] = bool(data.get('returned', True))
            c['returned_date'] = (data.get('returned_date') or
                                  (time.strftime('%Y-%m-%d') if c['returned'] else ''))
            save_checkouts(items)
        self.json_response(200, {'ok': True})

    def handle_camera_status(self):
        """AUTH: move a checkout through reserved -> out (picked up) -> returned.
        Returned records are kept as history (grayed in the UI), never deleted."""
        data = self._read_json_body()
        if data is None:
            return
        cid = str(data.get('id', ''))
        new = str(data.get('status', '')).strip().lower()
        if new not in ('reserved', 'out', 'returned'):
            self.json_response(400, {'error': 'status must be reserved, out, or returned'})
            return
        with _checkouts_lock:
            items = load_checkouts()
            c = self._find_checkout(items, cid)
            if not c:
                self.json_response(404, {'error': 'Checkout not found'})
                return
            stamp_status(c, new)
            save_checkouts(items)
        self.json_response(200, {'ok': True, 'status': new})

    def handle_camera_update(self):
        data = self._read_json_body()
        if data is None:
            return
        cid = str(data.get('id', ''))
        with _checkouts_lock:
            items = load_checkouts()
            c = self._find_checkout(items, cid)
            if not c:
                self.json_response(404, {'error': 'Checkout not found'})
                return
            for k in ('camera', 'out', 'due', 'status', 'picked_up_date', 'returned', 'returned_date',
                      'student_name', 'student_id', 'period', 'course',
                      'student_cell', 'parent_guardian', 'parent_cell', 'flag',
                      'kind', 'item', 'group', 'note', 'approved', 'pending'):
                if k in data:
                    c[k] = data[k]
            if 'status' in data:
                stamp_status(c, c.get('status'))   # normalize pickup/return dates to the new status
            if 'student_id' in data and str(data['student_id']).strip() != c.get('student_id'):
                c['student_id'] = str(data['student_id']).strip()
                r = resolve_student(c['student_id'])
                c['student_name'] = r['name']
                c['period'] = r['period']
                c['course'] = r['course']
                c['student_cell'] = r['student_cell']
                c['parent_guardian'] = r['parent_guardian']
                c['parent_cell'] = r['parent_cell']
                c['flag'] = '' if r['found'] else 'unknown student id'
            save_checkouts(items)
        self.json_response(200, {'ok': True})

    def handle_camera_delete(self):
        data = self._read_json_body()
        if data is None:
            return
        cid = str(data.get('id', ''))
        with _checkouts_lock:
            items = [x for x in load_checkouts() if x.get('id') != cid]
            save_checkouts(items)
        self.json_response(200, {'ok': True})

    # -- Canvas proxy -------------------------------------------------------

    def proxy_canvas(self, method):
        """Proxy a request to Canvas, injecting the server-side token."""
        if not CANVAS_TOKEN:
            self.json_response(503, {'error': 'Canvas token not configured'})
            return

        # Serve GETs from the short-lived in-memory cache (the dashboard re-pulls every course each
        # time it loads). The Refresh button sends X-Canvas-Fresh:1 to skip the cache and refill it.
        cache_key = self.path
        fresh = self.headers.get('X-Canvas-Fresh') == '1'
        if method == 'GET' and not fresh:
            _now = time.time()
            with _canvas_cache_lock:
                hit = _canvas_cache.get(cache_key)
            if hit and _now < hit[0]:
                _st, _ct, _lk, _body = hit[1], hit[2], hit[3], hit[4]
                self.send_response(_st)
                self.send_header('Content-Type', _ct)
                if _lk:
                    self.send_header('Link', _lk)
                self._cors_headers()
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Canvas-Cache', 'hit')
                self.send_header('Content-Length', len(_body))
                self.end_headers()
                self.wfile.write(_body)
                return

        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(CANVAS_HOST, context=ctx)
        headers = {
            'Authorization': f'Bearer {CANVAS_TOKEN}',
            'User-Agent': 'PVHS-Dashboard/2.0',
        }

        body = None
        if method in ('POST', 'PUT'):
            length = int(self.headers.get('Content-Length', 0))
            if length > 0:
                body = self.rfile.read(length)
            ct = self.headers.get('Content-Type', '')
            if ct:
                headers['Content-Type'] = ct

        try:
            conn.request(method, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            if resp.status >= 400:
                print(f'[canvas] {method} {self.path} -> {resp.status}: {resp_body[:200]}')

            ct = resp.getheader('Content-Type', 'application/json')
            link = resp.getheader('Link', '')
            if link:
                link = link.replace(f'https://{CANVAS_HOST}', '')
            if method == 'GET' and resp.status < 400:
                _now = time.time()
                with _canvas_cache_lock:
                    if len(_canvas_cache) > 300:   # prune expired entries so the cache stays bounded
                        for _k in [k for k, v in _canvas_cache.items() if v[0] < _now]:
                            _canvas_cache.pop(_k, None)
                    _canvas_cache[cache_key] = (_now + _CANVAS_CACHE_TTL, resp.status, ct, link, resp_body)

            self.send_response(resp.status)
            self.send_header('Content-Type', ct)
            if link:
                self.send_header('Link', link)
            self._cors_headers()
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Canvas-Cache', 'miss')
            self.send_header('Content-Length', len(resp_body))
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            self.json_response(502, {'error': str(e)})
        finally:
            conn.close()

    # -- Batch grade --------------------------------------------------------

    def handle_batch_grades(self):
        """Apply grades to multiple students for one assignment.

        Expects JSON body:
        {
          "course_id": "12345",
          "assignment_id": "67890",
          "grades": [
            {"student_id": "111", "score": 85, "comment": "Good work"},
            ...
          ]
        }
        """
        data = self._read_json_body()
        if data is None:
            return

        course_id = str(data.get('course_id', ''))
        assignment_id = str(data.get('assignment_id', ''))
        grades = data.get('grades', [])

        if not all([course_id, assignment_id, grades]):
            self.json_response(400, {'error': 'Missing course_id, assignment_id, or grades'})
            return

        results = []
        for g in grades:
            sid = str(g.get('student_id', ''))
            score = g.get('score')
            comment = g.get('comment', '')

            payload = {}
            if score is not None:
                payload['submission'] = {'posted_grade': str(score)}
            if comment:
                payload['comment'] = {'text_comment': comment}
            if not payload:
                results.append({'student_id': sid, 'ok': False, 'error': 'No score or comment'})
                continue

            resp = self._canvas_put(
                f'/api/v1/courses/{course_id}/assignments/{assignment_id}'
                f'/submissions/{sid}',
                payload,
            )
            results.append({
                'student_id': sid,
                'ok': resp is not None and isinstance(resp, dict),
                'grade': resp.get('grade') if isinstance(resp, dict) else None,
            })

        self.json_response(200, {'results': results})

    # -- Bulk comments ------------------------------------------------------

    def handle_bulk_comments(self):
        """Post the same comment to multiple students for one assignment.

        Expects JSON body:
        {
          "course_id": "12345",
          "assignment_id": "67890",
          "student_ids": ["111", "222", ...],
          "comment": "Please turn this in."
        }
        """
        data = self._read_json_body()
        if data is None:
            return

        course_id = str(data.get('course_id', ''))
        assignment_id = str(data.get('assignment_id', ''))
        student_ids = data.get('student_ids', [])
        comment = data.get('comment', '')

        if not all([course_id, assignment_id, student_ids, comment]):
            self.json_response(400, {'error': 'Missing required fields'})
            return

        results = []
        for sid in student_ids:
            sid = str(sid)
            payload = {'comment': {'text_comment': comment}}
            resp = self._canvas_put(
                f'/api/v1/courses/{course_id}/assignments/{assignment_id}'
                f'/submissions/{sid}',
                payload,
            )
            results.append({
                'student_id': sid,
                'ok': resp is not None,
            })

        self.json_response(200, {
            'comment': comment,
            'total': len(student_ids),
            'succeeded': sum(1 for r in results if r['ok']),
            'results': results,
        })

    # -- Canvas helpers -----------------------------------------------------

    def _canvas_put(self, path, payload):
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(CANVAS_HOST, context=ctx)
        headers = {
            'Authorization': f'Bearer {CANVAS_TOKEN}',
            'Content-Type': 'application/json',
            'User-Agent': 'PVHS-Dashboard/2.0',
        }
        body = json.dumps(payload).encode('utf-8')
        try:
            conn.request('PUT', path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status >= 400:
                print(f'[canvas] PUT {path} -> {resp.status}: {raw[:300]}')
            return json.loads(raw)
        except Exception as e:
            print(f'[canvas] PUT error: {e}')
            return None
        finally:
            conn.close()

    # -- File server --------------------------------------------------------

    def serve_file(self):
        path = urlparse(self.path).path.lstrip('/')
        if '..' in path:
            self.send_error(403)
            return
        filepath = os.path.join(SERVE_DIR, path)
        if not os.path.isfile(filepath):
            self.send_error(404)
            return
        mime, _ = mimetypes.guess_type(filepath)
        if mime is None:
            mime = 'application/octet-stream'
        with open(filepath, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', len(body))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    # -- Utilities ----------------------------------------------------------

    def _read_json_body(self, max_bytes=6 * 1024 * 1024):
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length > max_bytes:                       # cap the read so a huge body can't exhaust memory
                self.json_response(413, {'error': 'Request too large.'})
                return None
            return json.loads(self.rfile.read(length))
        except Exception:
            self.json_response(400, {'error': 'Invalid request.'})   # no internal detail echoed
            return None

    def json_response(self, status, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self._cors_headers()
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers',
                         'Authorization, Content-Type, X-API-Key, X-Camera-Key')
        self.send_header('Access-Control-Max-Age', '86400')

    def log_message(self, fmt, *args):
        msg = fmt % args
        if '/api/' in msg or '/batch/' in msg or '/auth/' in msg:
            print(f'  [server] {msg}')


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == '__main__':
    print(f'PVHS Dashboard Server v2.0 on port {PORT}')
    print(f'  Canvas host: {CANVAS_HOST}')
    print(f'  Canvas token: {"configured (" + str(len(CANVAS_TOKEN)) + " chars)" if CANVAS_TOKEN else "NOT SET"}')
    print(f'  Allowed emails: {ALLOWED_EMAILS or "(any authenticated user)"}')
    print(f'  API key: {"configured" if API_KEY else "not set"}')
    print(f'  Camera PIN: {"configured" if CAMERA_PIN else "NOT SET (manager page will 401)"}')
    if CANVAS_TOKEN:
        import urllib.request
        try:
            rq = urllib.request.Request(
                f'https://{CANVAS_HOST}/api/v1/users/self',
                headers={
                    'Authorization': f'Bearer {CANVAS_TOKEN}',
                    'User-Agent': 'PVHS-Dashboard/2.0',
                },
            )
            with urllib.request.urlopen(rq, timeout=10) as r:
                print(f'  Canvas token test: OK ({r.status})')
        except Exception as e:
            print(f'  Canvas token test: FAILED ({e})')
    server = ThreadedHTTPServer(('0.0.0.0', PORT), Handler)
    server.serve_forever()

# deploy trigger 2026-09-28: ensure garcia_roster endpoint is live
