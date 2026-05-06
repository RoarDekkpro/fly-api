import os
import re
import threading
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

app = Flask(__name__)
CORS(app)


def fetch_flights(tfs):
    with _lock:
        # Forsøk 1: uten SOCS (fungerer for de fleste ruter på US-servere)
        _ff_core.fetch = _fetch_plain
        try:
            result = get_flights_from_filter(tfs, currency='NOK')
            if result.flights:
                return result
        except Exception:
            pass

        # Forsøk 2: med SOCS-cookie (hjelper for noen ruter)
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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
