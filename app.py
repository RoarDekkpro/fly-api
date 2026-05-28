import os
import re
import subprocess
import threading
import time
import fast_flights.core as _ff_core
from fast_flights.primp import Client as _PrimpClient

_SOCS = 'CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg'
_lock = threading.Lock()

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
from fast_flights import FlightData, Passengers, create_filter, get_flights_from_filter


def _ensure_chromium():
    """Install Playwright Chromium at startup if the binary is missing."""
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

_ensure_chromium()

app = Flask(__name__)
CORS(app)


# ── GOOGLE FLIGHTS ──────────────────────────────────────────────────────────

def fetch_flights(tfs):
    with _lock:
        _ff_core.fetch = _fetch_plain
        try:
            result = get_flights_from_filter(tfs, currency='NOK')
            if result.flights:
                return result
        except Exception:
            pass
        _ff_core.fetch = _fetch_socs
        try:
            result = get_flights_from_filter(tfs, currency='NOK')
            if result.flights:
                return result
        except Exception:
            pass
        return None


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
        tfs = create_filter(
            flight_data=[FlightData(date=date, from_airport=origin, to_airport=destination)],
            trip='one-way',
            seat='economy',
            passengers=Passengers(adults=1),
        )
        result = fetch_flights(tfs)

        if result is None:
            return jsonify({'no_results': True, 'origin': origin, 'destination': destination, 'date': date})

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
        ]

        return jsonify({
            'flights':       flights,
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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
