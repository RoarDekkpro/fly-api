import os
import re
import fast_flights.core as _ff_core
from fast_flights.primp import Client as _PrimpClient

# Bypass EU GDPR consent page by pre-setting the SOCS cookie
_orig_fetch = _ff_core.fetch
def _fetch_with_consent(params):
    client = _PrimpClient(impersonate='chrome_126', verify=False)
    client.set_cookies('https://www.google.com', {
        'SOCS': 'CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg'
    })
    res = client.get('https://www.google.com/travel/flights', params=params)
    assert res.status_code == 200
    return res
_ff_core.fetch = _fetch_with_consent

from flask import Flask, jsonify, request
from flask_cors import CORS
from fast_flights import FlightData, Passengers, get_flights

app = Flask(__name__)
CORS(app)


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
        return jsonify({'error': 'Ugyldig destinasjonskode — bruk 3 bokstaver (eks: AMS)'}), 400
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({'error': 'Ugyldig dato — bruk YYYY-MM-DD'}), 400

    try:
        result = get_flights(
            flight_data=[FlightData(date=date, from_airport=origin, to_airport=destination)],
            trip='one-way',
            seat='economy',
            passengers=Passengers(adults=1),
        )

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
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
