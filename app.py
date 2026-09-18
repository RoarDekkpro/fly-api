import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from fast_flights.core import parse_response as _parse_ff_response
from fast_flights.primp import Client as _PrimpClient

_SOCS = 'CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg'

def _fetch_plain(params):
    client = _PrimpClient(impersonate='chrome_126', verify=False)
    res = client.get('https://www.google.com/travel/flights', params=params)
    assert res.status_code == 200
    return res

def _fetch_socs(params):
    client = _PrimpClient(impersonate='chrome_126', verify=False)
    client.set_cookies('https://www.google.com', {'SOCS': _SOCS})
    res = client.get('https://www.google.com/travel/flights', params=params)
    assert res.status_code == 200
    return res

from flask import Flask, jsonify, request
from flask_cors import CORS
from fast_flights import FlightData, Passengers, create_filter


_chromium_lock  = threading.Lock()
_chromium_ready = False

def _ensure_chromium():
    """Install Playwright Chromium if the binary is missing — only needed by
    the SAS EuroBonus scraper, never by the core Google Flights search/route
    endpoints. Called lazily, on first actual use of a SAS endpoint, NOT at
    app startup: there's no persistent disk on this plan, so every fresh
    deploy would otherwise re-download ~190MB of browser binaries every
    time, which — done eagerly, even backgrounded in a thread — repeatedly
    crash-looped the entire gunicorn process (almost certainly OOM on this
    plan's memory limit) and took the whole app down, not just the SAS
    feature that actually needs it."""
    global _chromium_ready
    if _chromium_ready:
        return
    with _chromium_lock:
        if _chromium_ready:
            return
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            exe = pw.chromium.executable_path
            pw.stop()
            if not os.path.exists(exe):
                raise FileNotFoundError(exe)
        except Exception:
            subprocess.run(['python', '-m', 'playwright', 'install', 'chromium'],
                           check=False, capture_output=False)
        _chromium_ready = True

app = Flask(__name__)
CORS(app)


# ── GOOGLE FLIGHTS ──────────────────────────────────────────────────────────

def _has_usable_flights(result):
    # A non-empty result can still be all price-only "separate tickets" rows
    # with no departure/arrival — that happens when the consent page wasn't
    # actually accepted, and is as useless as an empty result.
    return bool(result and result.flights and any(fl.departure for fl in result.flights))


def _fetch_and_parse(params):
    """Fetch + parse a single Google Flights query, plain first then with the
    SOCS consent cookie. Each call owns its own primp Client, so — unlike the
    old approach of monkeypatching fast_flights.core.fetch under a lock — this
    is safe to run concurrently across threads, which /route needs to stay
    under the ~30s proxy timeout when fanning out to multiple hubs."""
    for fetcher in (_fetch_plain, _fetch_socs):
        try:
            result = _parse_ff_response(fetcher(params))
            if _has_usable_flights(result):
                return result
        except Exception:
            pass
    return None


def _parse_price(price_str):
    if not price_str:
        return None
    digits = re.sub(r'[^\d]', '', price_str)
    return int(digits) if digits else None


def _serialize_flights(result, limit=None):
    if result is None:
        return []
    flights = [
        {
            'is_best':            fl.is_best,
            'name':               fl.name,
            'departure':          fl.departure,
            'arrival':            fl.arrival,
            'arrival_time_ahead': fl.arrival_time_ahead or '',
            'duration':           fl.duration,
            'stops':              fl.stops,
            'delay':              fl.delay,
            'price':              fl.price,
        }
        for fl in result.flights
        # Google Flights lists some "separate tickets / self-transfer" combos as
        # price-only summary rows with no schedule — often the cheapest of all,
        # which would otherwise flood the top of a price-sorted list unusably.
        if fl.departure and fl.arrival
    ]
    seen = set()
    deduped = []
    for f in flights:
        key = (f['name'], f['departure'], f['arrival'], f['price'])
        if key not in seen:
            seen.add(key)
            deduped.append(f)
    flights = deduped
    if limit is not None:
        # Cheapest first — price is this app's proxy for seat availability/belegg.
        flights.sort(key=lambda f: (_parse_price(f['price']) is None, _parse_price(f['price']) or 0))
        flights = flights[:limit]
    return flights


def _search_leg(origin, destination, date):
    """One-way Google Flights search for a single origin/destination/date leg.
    Returns a fast_flights Result, or None on no-results/error."""
    try:
        tfs = create_filter(
            flight_data=[FlightData(date=date, from_airport=origin, to_airport=destination)],
            trip='one-way',
            seat='economy',
            passengers=Passengers(adults=1),
        )
        params = {'tfs': tfs.as_b64().decode('utf-8'), 'hl': 'en', 'tfu': 'EgQIABABIgA', 'curr': 'NOK'}
        return _fetch_and_parse(params)
    except Exception:
        return None


# Major connection hubs checked when fanning out a route search. Kept short —
# each hub costs up to 3 Google Flights fetches (leg1 + leg2 same/next day).
CONNECTION_HUBS = ['CPH', 'ARN', 'AMS', 'FRA', 'LHR', 'IST']


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


@app.route('/search')
def search():
    origin      = request.args.get('origin', '').upper().strip()
    destination = request.args.get('destination', '').upper().strip()
    date        = request.args.get('date', '').strip()

    if not re.match(r'^[A-Z]{3}$', origin):
        return jsonify({'error': 'Ugyldig avgangskode — bruk 3 bokstaver (eks: OSL)'}), 400
    if not re.match(r'^[A-Z]{3}$', destination):
        return jsonify({'error': 'Ugyldig destinasjonskode — bruk 3 bokstaver (eks: FCO)'}), 400
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({'error': 'Ugyldig dato — bruk YYYY-MM-DD'}), 400

    try:
        result = _search_leg(origin, destination, date)

        if result is None:
            return jsonify({'no_results': True, 'origin': origin, 'destination': destination, 'date': date})

        return jsonify({
            'flights':       _serialize_flights(result),
            'current_price': result.current_price or '',
            'origin':        origin,
            'destination':   destination,
            'date':          date,
        })

    except Exception as e:
        msg = str(e)
        if 'No flights found' in msg:
            return jsonify({'no_results': True, 'origin': origin, 'destination': destination, 'date': date})
        return jsonify({'error': f'Søkefeil: {msg[:200]}'}), 500


@app.route('/route')
def route_search():
    """Fan-out route search: direct A→B, plus each hub's first leg (A→hub),
    via a fixed set of major hubs — so the client can offer alternative first
    legs instead of only Google's single best-guess itinerary. Onward (hub→B)
    options are NOT included here; the client fetches those lazily per hub
    from /onward only when a dropdown is actually opened. Fetching onward
    options for every hub on every search (previous design) meant up to ~16
    sequential-equivalent Google Flights fetches per request, which blew past
    this Render plan's hard ~30s proxy timeout even after parallelizing —
    most of that work was wasted since a user only expands 1-2 hubs."""
    origin      = request.args.get('origin', '').upper().strip()
    destination = request.args.get('destination', '').upper().strip()
    date        = request.args.get('date', '').strip()
    max_hubs    = min(max(int(request.args.get('max_hubs', len(CONNECTION_HUBS)) or len(CONNECTION_HUBS)), 0), len(CONNECTION_HUBS))

    if not re.match(r'^[A-Z]{3}$', origin):
        return jsonify({'error': 'Ugyldig avgangskode — bruk 3 bokstaver (eks: OSL)'}), 400
    if not re.match(r'^[A-Z]{3}$', destination):
        return jsonify({'error': 'Ugyldig destinasjonskode — bruk 3 bokstaver (eks: FCO)'}), 400
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({'error': 'Ugyldig dato — bruk YYYY-MM-DD'}), 400

    try:
        next_date = (datetime.strptime(date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'Ugyldig dato — bruk YYYY-MM-DD'}), 400

    candidate_hubs = [h for h in CONNECTION_HUBS if h not in (origin, destination)][:max_hubs]

    # Direct + every hub's first leg, all in parallel.
    with ThreadPoolExecutor(max_workers=max(1, len(candidate_hubs) + 1)) as pool:
        direct_future = pool.submit(_search_leg, origin, destination, date)
        leg1_futures  = {hub: pool.submit(_search_leg, origin, hub, date) for hub in candidate_hubs}
        direct_result = direct_future.result()
        leg1_results  = {hub: f.result() for hub, f in leg1_futures.items()}

    direct = _serialize_flights(direct_result, limit=60)
    connections = [
        {'hub': hub, 'leg1': _serialize_flights(leg1_results[hub], limit=40)}
        for hub in candidate_hubs
        if _has_usable_flights(leg1_results[hub])
    ]

    return jsonify({
        'origin':      origin,
        'destination': destination,
        'date':        date,
        'next_date':   next_date,
        'direct':      direct,
        'connections': connections,
    })


@app.route('/onward')
def onward_search():
    """Onward (hub->destination) options for one hub — same day and next day,
    fetched lazily only when the client expands that hub's dropdown."""
    hub         = request.args.get('hub', '').upper().strip()
    destination = request.args.get('destination', '').upper().strip()
    date        = request.args.get('date', '').strip()

    if not re.match(r'^[A-Z]{3}$', hub):
        return jsonify({'error': 'Ugyldig hub-kode'}), 400
    if not re.match(r'^[A-Z]{3}$', destination):
        return jsonify({'error': 'Ugyldig destinasjonskode'}), 400
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({'error': 'Ugyldig dato'}), 400

    try:
        next_date = (datetime.strptime(date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'Ugyldig dato'}), 400

    with ThreadPoolExecutor(max_workers=2) as pool:
        same_future = pool.submit(_search_leg, hub, destination, date)
        next_future = pool.submit(_search_leg, hub, destination, next_date)
        same_result = same_future.result()
        next_result = next_future.result()

    return jsonify({
        'hub':           hub,
        'destination':   destination,
        'leg2_same_day': _serialize_flights(same_result, limit=60),
        'leg2_next_day': _serialize_flights(next_result, limit=60),
    })


# ── SAS EUROBONUS SCRAPER ────────────────────────────────────────────────────

_SAS_CACHE   = {}
_SAS_TTL     = 300   # 5 min
_SAS_LOCK    = threading.Semaphore(1)  # only one Chromium at a time

# Regexes for SAS page text parsing
_TIME_RE  = re.compile(r'(\d{2}:\d{2})\s*[–—\-]\s*(\d{2}:\d{2})')
_DUR_RE   = re.compile(r'(\d+)\s*t(?:\s*(\d+)\s*m)?')
_STOPS_RE = re.compile(r'(\d+)\s*stopp', re.I)
_CABIN_RE = re.compile(
    r'(Business Plus|Business|First|Economy|Premium)\s*(?:\d+\s*igjen\s*)?[•●·]?\s*([\d][\d \s]*)\s*(?:p\b|poeng)',
    re.I
)


def _pts(s):
    try:
        return int(re.sub(r'[\s ]', '', s))
    except Exception:
        return None

def _is_standard(pts):
    return pts is not None and pts > 0 and pts % 1000 == 0


def _parse_page_text(text):
    """Parse the SAS booking page inner text into structured flight rows."""
    flights = []
    matches = list(_TIME_RE.finditer(text))
    if not matches:
        return flights

    for i, tm in enumerate(matches):
        start = tm.start()
        end   = matches[i + 1].start() if i + 1 < len(matches) else start + 900
        block = text[start:end]

        dep_t = tm.group(1)
        arr_t = tm.group(2)

        sm    = _STOPS_RE.search(block)
        stops = int(sm.group(1)) if sm else 0
        if re.search(r'direkte', block, re.I):
            stops = 0

        dur_m    = _DUR_RE.search(block)
        duration = (f"{dur_m.group(1)}t {dur_m.group(2)}m"
                    if dur_m and dur_m.group(2) and int(dur_m.group(2)) > 0
                    else (f"{dur_m.group(1)}t" if dur_m else ''))

        eco = prem = biz = fst = None
        for cm in _CABIN_RE.finditer(block):
            cab = cm.group(1).lower().replace(' ', '')
            pts = _pts(cm.group(2))
            if   cab == 'economy':                    eco  = pts
            elif cab == 'premium':                    prem = pts
            elif cab in ('business', 'businessplus'): biz  = pts
            elif cab == 'first':                      fst  = pts

        if any(_is_standard(p) for p in (eco, prem, biz, fst)):
            flights.append({
                'departure':    dep_t,
                'arrival':      arr_t,
                'duration':     duration,
                'stops':        stops,
                'economy_pts':  eco,
                'premium_pts':  prem,
                'business_pts': biz,
                'first_pts':    fst,
            })

    return flights


def _scrape_sas(origin, dest, date_str):
    from playwright.sync_api import sync_playwright

    _ensure_chromium()

    date_sas = date_str.replace('-', '')
    url = (f'https://www.sas.no/book/flights/'
           f'?search=OW_{origin}-{dest}-{date_sas}_a1c0i0y0'
           f'&view=upsell&bookingFlow=points&sortBy=rec&filterBy=all')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
                '--single-process', '--no-zygote', '--disable-setuid-sandbox',
                '--disable-extensions', '--disable-background-networking',
                '--disable-default-apps', '--mute-audio', '--no-first-run',
                '--disable-hang-monitor', '--disable-sync',
            ]
        )
        ctx = browser.new_context(
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            locale='nb-NO',
            timezone_id='Europe/Oslo',
            viewport={'width': 1280, 'height': 900},
        )
        page = ctx.new_page()

        # Block images, fonts, media to reduce memory usage on low-RAM servers
        page.route('**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,eot,mp4,webm,avif}',
                   lambda route: route.abort())

        try:
            page.goto(url, wait_until='domcontentloaded', timeout=60000)

            # Dismiss cookie consent if present
            for selector in [
                'button:has-text("Godta alle")',
                'button:has-text("Aksepter alle")',
                'button:has-text("Accept all")',
                '[id*="onetrust"] button.accept',
            ]:
                try:
                    page.click(selector, timeout=4000)
                    page.wait_for_timeout(1000)
                    break
                except Exception:
                    pass

            # Wait for flight results to appear
            try:
                page.wait_for_selector(
                    '[class*="OfferList"], [class*="offer-list"], '
                    '[class*="FlightList"], [class*="flight-list"], '
                    '[class*="flightCard"], [class*="FlightCard"], '
                    '[class*="journey"], [class*="Journey"], '
                    '[class*="result-item"], [class*="ResultItem"]',
                    timeout=30000
                )
            except Exception:
                page.wait_for_timeout(15000)

            text = page.inner_text('body')

        finally:
            browser.close()

    return _parse_page_text(text)


def get_sas_bonus(origin, dest, date):
    key = f'{origin}|{dest}|{date}'
    if key in _SAS_CACHE:
        val, ts = _SAS_CACHE[key]
        if time.time() - ts < _SAS_TTL:
            return val, None

    acquired = _SAS_LOCK.acquire(blocking=True, timeout=90)
    if not acquired:
        return [], 'Busy — try again shortly'
    try:
        result = _scrape_sas(origin, dest, date)
        err    = None
    except Exception as e:
        result = []
        err    = str(e)[:200]
    finally:
        _SAS_LOCK.release()

    _SAS_CACHE[key] = (result, time.time())
    return result, err


@app.route('/sas-bonus')
def sas_bonus():
    origin = request.args.get('origin', '').upper().strip()
    dest   = request.args.get('destination', '').upper().strip()
    date   = request.args.get('date', '').strip()

    if not re.match(r'^[A-Z]{3}$', origin):
        return jsonify({'error': 'Invalid origin'}), 400
    if not re.match(r'^[A-Z]{3}$', dest):
        return jsonify({'error': 'Invalid destination'}), 400
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({'error': 'Invalid date'}), 400

    date_sas = date.replace('-', '')
    sas_url  = (f'https://www.sas.no/book/flights/'
                f'?search=OW_{origin}-{dest}-{date_sas}_a1c0i0y0'
                f'&view=upsell&bookingFlow=points&sortBy=rec&filterBy=all')

    flights, err = get_sas_bonus(origin, dest, date)
    return jsonify({
        'flights':     flights,
        'sas_url':     sas_url,
        'origin':      origin,
        'destination': dest,
        'date':        date,
        'error':       err,
    })


@app.route('/sas-debug')
def sas_debug():
    origin = request.args.get('origin', 'OSL').upper().strip()
    dest   = request.args.get('destination', 'CPH').upper().strip()
    date   = request.args.get('date', '2026-06-01').strip()

    from playwright.sync_api import sync_playwright
    _ensure_chromium()
    date_sas = date.replace('-', '')
    url = (f'https://www.sas.no/book/flights/'
           f'?search=OW_{origin}-{dest}-{date_sas}_a1c0i0y0'
           f'&view=upsell&bookingFlow=points&sortBy=rec&filterBy=all')

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu',
                      '--single-process','--no-zygote','--disable-setuid-sandbox',
                      '--disable-extensions']
            )
            ctx = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                locale='nb-NO', timezone_id='Europe/Oslo',
                viewport={'width': 1280, 'height': 900},
            )
            page = ctx.new_page()
            page.route('**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,eot,mp4,webm}',
                       lambda route: route.abort())
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
            page.wait_for_timeout(15000)
            text = page.inner_text('body')
            browser.close()

        matches_time  = len(_TIME_RE.findall(text))
        matches_cabin = len(_CABIN_RE.findall(text))
        return jsonify({
            'url':           url,
            'text_length':   len(text),
            'time_matches':  matches_time,
            'cabin_matches': matches_cabin,
            'text_sample':   text[:3000],
        })
    except Exception as e:
        return jsonify({'error': str(e)[:500]}), 500


@app.route('/route-debug')
def route_debug():
    """Temporary: _search_leg() swallows exceptions silently, so /route gives
    no signal when Google Flights fetches fail outright. This bypasses that
    to show the real status/error per fetch mode."""
    origin = request.args.get('origin', 'SVG').upper().strip()
    dest   = request.args.get('destination', 'FCO').upper().strip()
    date   = request.args.get('date', '2026-09-28').strip()

    tfs = create_filter(
        flight_data=[FlightData(date=date, from_airport=origin, to_airport=dest)],
        trip='one-way', seat='economy', passengers=Passengers(adults=1),
    )
    params = {'tfs': tfs.as_b64().decode('utf-8'), 'hl': 'en', 'tfu': 'EgQIABABIgA', 'curr': 'NOK'}

    out = {}
    try:
        res = _fetch_plain(params)
        out['plain_status'] = res.status_code
        out['plain_len']    = len(res.text)
    except Exception as e:
        out['plain_error'] = f'{type(e).__name__}: {e}'[:500]

    try:
        res = _fetch_socs(params)
        out['socs_status'] = res.status_code
        out['socs_len']    = len(res.text)
    except Exception as e:
        out['socs_error'] = f'{type(e).__name__}: {e}'[:500]

    return jsonify(out)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
