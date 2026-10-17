from pathlib import Path
from flask import Flask, request, jsonify

# Import internal modules (repo root must be importable)
import sys
sys.path.append(str(Path(__file__).resolve().parents[1]))  # repository root

from plantreadiness import db as dbm
from plantreadiness import calibration as calib_mod
from plantreadiness.repository import SQLiteRepository
from plantreadiness import services as svc
from plantreadiness.db import (DuplicateRecordError, NotFoundError,
                               ValidationError)
from prediction import predict_for_plant

app = Flask(__name__)

# Local SQLite database used by the Python backend.
DB_PATH = Path('plant_readiness.db')
MODELS_DIR = Path('models')


def _error_response(exc):
    """Map workflow errors to HTTP status codes with a JSON body."""
    if isinstance(exc, NotFoundError):
        return jsonify({'error': str(exc)}), 404
    if isinstance(exc, DuplicateRecordError):
        return jsonify({'error': str(exc)}), 409
    if isinstance(exc, ValidationError):
        return jsonify({'error': str(exc)}), 400
    return jsonify({'error': str(exc)}), 500


def _snapshot_result(repo, result, **kwargs):
    """Best-effort append-only snapshot; never break the response on failure."""
    try:
        return svc.save_prediction_snapshot(repo, result, **kwargs)
    except Exception:
        return None


def _clean_variety(value):
    """Normalize UI variety values ('All varieties' etc.) to None."""
    if value is None:
        return None
    value = str(value).strip()
    if not value or value.lower() in ('all varieties', 'all', 'any'):
        return None
    return value


def _clean_date(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


@app.route('/plants', methods=['GET'])
def list_plants():
    conn = dbm.connect(DB_PATH)
    try:
        rows = dbm.list_plants(conn)
        plants = []
        for r in rows:
            plants.append({
                'plant_id': r['plant_id'],
                'species': r['species'],
                'variety': r['variety'],
                'external_ref': r['external_ref'],
                'cohort': r['cohort'],
                'data_source': r['data_source']
            })
        return jsonify(plants)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/register', methods=['POST'])
def register_plant():
    data = request.get_json()
    if not data or 'species' not in data:
        return jsonify({'error': 'Missing required field(s): species'}), 400

    species = data.get('species')
    variety = data.get('variety')
    date_started = data.get('date_started')
    data_source = data.get('data_source', 'real')
    growing_conditions = data.get('growing_conditions')
    external_ref = data.get('external_ref')
    cohort = data.get('cohort')

    conn = dbm.connect(DB_PATH)
    try:
        plant_id = dbm.register_plant(
            conn,
            species=species,
            variety=variety,
            date_started=date_started,
            data_source=data_source,
            growing_conditions=growing_conditions,
            external_ref=external_ref,
            cohort=cohort,
        )
        return jsonify({'plant_id': plant_id}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    finally:
        conn.close()


@app.route('/predict', methods=['POST'])
def predict_plant():
    """Legacy plant-specific prediction endpoint (kept for compatibility)."""
    data = request.get_json()
    if not data or 'plantId' not in data:
        return jsonify({'error': 'Missing plantId'}), 400

    try:
        plant_id = int(data['plantId'])
    except (TypeError, ValueError):
        return jsonify({'error': 'plantId must be an integer'}), 400

    allow_synthetic = bool(data.get('allow_synthetic', False))
    allow_provisional = bool(data.get('allow_provisional_baseline', False))
    conn = dbm.connect(DB_PATH)
    try:
        repo = SQLiteRepository(conn)
        result = predict_for_plant(conn, plant_id, MODELS_DIR,
                                   allow_synthetic=allow_synthetic,
                                   allow_provisional_baseline=allow_provisional)
        _snapshot_result(repo, result, plant_id=plant_id,
                         prediction_scope='plant')
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/species', methods=['GET'])
def species_catalog():
    """Species inventory for the dashboard dropdown."""
    conn = dbm.connect(DB_PATH)
    try:
        catalog = dbm.species_dashboard_catalog(conn, data_source='real')
        for item in catalog:
            species = item.get('species')
            if not species:
                continue
            item['readiness_event_counts'] = dbm.readiness_event_counts(
                conn, species, None, data_source='real')

        # Configured species (e.g. Marigold (Apsara Yellow)) may have no
        # observations yet - they still appear in the dropdown so the user can
        # select them and see "insufficient evidence" rather than being hidden.
        # No placeholder plant is created; the entry simply has zero counts.
        from plantreadiness import config as _cfg
        present = {item.get('species') for item in catalog}
        for species in getattr(_cfg, 'CONFIGURED_SPECIES', ()):
            if species in present:
                continue
            catalog.append({
                'species': species,
                'variety': '',
                'plant_count': 0,
                'observation_count': 0,
                'latest_observation_date': None,
                'varieties': [],
                'available_dates': [],
                'readiness_event_counts': dbm.readiness_event_counts(
                    conn, species, None, data_source='real'),
            })

        catalog.sort(
            key=lambda item: (
                item.get('latest_observation_date') or '',
                item.get('observation_count') or 0,
                item.get('species') or '',
            ),
            reverse=True,
        )
        return jsonify(catalog)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/predict/species', methods=['POST'])
def predict_species():
    """Species-level prediction from the latest available cloud cohort."""
    data = request.get_json() or {}
    species = str(data.get('species', '')).strip()
    if not species:
        return jsonify({'error': 'species is required'}), 400

    variety = _clean_variety(data.get('variety'))
    observation_date = _clean_date(
        data.get('observation_date') or data.get('date'))
    allow_synthetic = bool(data.get('allow_synthetic', False))
    # The species endpoint labels provisional baseline estimates explicitly,
    # so it may fall back to the reference baseline when no model is available.
    allow_provisional = bool(data.get('allow_provisional_baseline', True))

    conn = dbm.connect(DB_PATH)
    try:
        repo = SQLiteRepository(conn)
        # Pull any new ESP32-CAM images for this species first, so the
        # prediction reflects the latest cloud measurements. Bounded and
        # fail-open: on any problem the prediction proceeds on stored data.
        camera_refresh_report = svc.refresh_camera_observations(
            repo, species=species)
        result = svc.predict_species_cohort(
            repo,
            species,
            variety,
            MODELS_DIR,
            observation_date=observation_date,
            allow_synthetic=allow_synthetic,
            allow_provisional_baseline=allow_provisional,
        )
        _snapshot_result(repo, result, prediction_scope='species_cohort',
                         cohort_snapshot=result.get('cohort_snapshot'))
        result['camera_refresh'] = camera_refresh_report
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/camera/refresh', methods=['POST'])
def camera_refresh():
    """Pull/measure/import new ESP32-CAM images (all species unless given)."""
    data = request.get_json(silent=True) or {}
    species = str(data.get('species', '')).strip() or None

    conn = dbm.connect(DB_PATH)
    try:
        repo = SQLiteRepository(conn)
        return jsonify(svc.refresh_camera_observations(repo, species=species))
    except Exception as e:
        return _error_response(e)
    finally:
        conn.close()


@app.route('/api/reference', methods=['GET'])
def reference_baseline():
    """Successful-cohort reference baseline (built on demand with ?build=1)."""
    species = str(request.args.get('species', '')).strip()
    if not species:
        return jsonify({'error': 'species is required'}), 400
    variety = _clean_variety(request.args.get('variety'))
    build = request.args.get('build', '0') in ('1', 'true', 'yes')

    conn = dbm.connect(DB_PATH)
    try:
        repo = SQLiteRepository(conn)
        if build:
            baseline = svc.build_reference_baseline(
                repo, species, models_dir=MODELS_DIR, variety=variety)
        else:
            baseline = svc.load_reference_baseline(
                MODELS_DIR, species, variety)
            if baseline is None:
                return jsonify({
                    'error': 'No reference baseline has been built yet.',
                    'hint': 'Request /api/reference?build=1 to build one.',
                }), 404
        return jsonify(baseline)
    except Exception as e:
        return _error_response(e)
    finally:
        conn.close()


@app.route('/api/calibration', methods=['GET'])
def calibration_status():
    """Report the current rod-length verification state and the quarantine."""
    conn = dbm.connect(DB_PATH)
    try:
        repo = SQLiteRepository(conn)
        from plantreadiness import config as _cfg
        state = repo.current_calibration_setting()
        desc = calib_mod.describe(state, _cfg.CAMERA_ROD_LENGTH_CM)
        # quarantine summary over stored observations
        rows = [dict(r) for r in conn.execute(
            "SELECT calibration_source, rod_length_cm, calibration_verified "
            "FROM observations")]
        summary = calib_mod.summarise_groups(rows)
        return jsonify({
            "calibration": desc,
            "observations_by_calibration_group": summary,
            "history": repo.calibration_settings_history(limit=10),
        })
    except Exception as e:
        return _error_response(e)
    finally:
        conn.close()


@app.route('/api/reference/build', methods=['POST'])
def build_reference():
    """Build (and persist) the reference baseline from stored measurements."""
    data = request.get_json() or {}
    species = str(data.get('species', '')).strip()
    if not species:
        return jsonify({'error': 'species is required'}), 400
    variety = _clean_variety(data.get('variety'))

    conn = dbm.connect(DB_PATH)
    try:
        repo = SQLiteRepository(conn)
        baseline = svc.build_reference_baseline(
            repo, species, models_dir=MODELS_DIR, variety=variety)
        return jsonify(baseline)
    except Exception as e:
        return _error_response(e)
    finally:
        conn.close()


@app.route('/api/import', methods=['POST'])
def import_dataset():
    """Import a measurement CSV and (on success) build the reference baseline.

    Accepts either a multipart upload (field ``file``) or a JSON body with
    ``csv_text``/``csv``. Required: ``species``. Optional: ``variety``,
    ``date_started``. The whole file must validate before anything is written
    (all-or-nothing); on success the reference baseline is rebuilt so
    predictions work from the first day.
    """
    # Resolve CSV text from either transport.
    csv_text = None
    species = ''
    variety = None
    date_started = None
    if request.files and 'file' in request.files:
        f = request.files['file']
        try:
            csv_text = f.read().decode('utf-8-sig')
        except UnicodeDecodeError:
            return jsonify({'error': 'file is not valid UTF-8 text'}), 400
        species = (request.form.get('species') or '').strip()
        variety = _clean_variety(request.form.get('variety'))
        date_started = request.form.get('date_started')
    else:
        data = request.get_json(silent=True) or {}
        csv_text = data.get('csv_text') or data.get('csv')
        species = str(data.get('species', '')).strip()
        variety = _clean_variety(data.get('variety'))
        date_started = data.get('date_started')
    if not csv_text or not str(csv_text).strip():
        return jsonify({'error': 'csv text is required (field "file" or "csv_text")'}), 400
    if not species:
        return jsonify({'error': 'species is required'}), 400

    conn = dbm.connect(DB_PATH)
    try:
        repo = SQLiteRepository(conn)
        report = svc.run_csv_import(
            repo, str(csv_text), species=species, variety=variety,
            date_started=date_started, dry_run=False)
        if report.get('issues'):
            return jsonify(report), 400
        resolved_species = report.get('species') or species
        # Auto-build the reference baseline so predictions work from day 1.
        try:
            baseline = svc.build_reference_baseline(
                repo, resolved_species, models_dir=MODELS_DIR, variety=variety)
            report['baseline'] = baseline
        except Exception as exc:  # baseline build is best-effort; import succeeded
            report['baseline_error'] = str(exc)
        return jsonify(report)
    except Exception as e:
        return _error_response(e)
    finally:
        conn.close()


@app.route('/api/assessments', methods=['GET', 'POST'])
def readiness_assessments():
    """Read or record the user's readiness judgement."""
    conn = dbm.connect(DB_PATH)
    try:
        repo = SQLiteRepository(conn)
        if request.method == 'GET':
            species = _clean_date(request.args.get('species'))
            plant_id = request.args.get('plant_id')
            plant_id = int(plant_id) if plant_id else None
            return jsonify(svc.list_readiness_assessments(
                repo, plant_id=plant_id, species=species))
        data = request.get_json() or {}
        status = data.get('readiness_status') or data.get('status')
        assessment_date = data.get('assessment_date') or data.get('date')
        if not status or not assessment_date:
            return jsonify({
                'error': 'assessment_date and readiness_status are required',
            }), 400
        plant_id = data.get('plant_id')
        plant_id = int(plant_id) if plant_id else None
        assessment = svc.record_readiness_assessment(
            repo,
            plant_id=plant_id,
            species=data.get('species'),
            variety=_clean_variety(data.get('variety')),
            assessment_date=assessment_date,
            readiness_status=status,
            notes=data.get('notes'),
            prediction_id=(int(data['prediction_id'])
                           if data.get('prediction_id') else None),
            assessment_source=data.get('assessment_source', 'user'),
        )
        return jsonify(assessment), 201
    except Exception as e:
        return _error_response(e)
    finally:
        conn.close()


@app.route('/api/transplant', methods=['GET', 'POST'])
def transplants():
    """Record a transplant (or a decision that one did not occur)."""
    conn = dbm.connect(DB_PATH)
    try:
        repo = SQLiteRepository(conn)
        if request.method == 'GET':
            plant_id = request.args.get('plant_id')
            plant_id = int(plant_id) if plant_id else None
            return jsonify(repo.all_feedback(plant_id=plant_id))
        data = request.get_json() or {}
        plant_id = data.get('plant_id')
        transplant_date = data.get('transplant_date') or data.get('date')
        if plant_id is None or not transplant_date:
            return jsonify({
                'error': 'plant_id and transplant_date are required',
            }), 400
        record = svc.record_transplant(
            repo,
            plant_id=int(plant_id),
            transplant_date=transplant_date,
            height_at_transplant_cm=data.get('height_at_transplant_cm'),
            transplant_occurred=bool(data.get('transplant_occurred', True)),
            condition_at_transplant=data.get('condition_at_transplant'),
            transplant_notes=data.get('transplant_notes'),
        )
        return jsonify(record), 201
    except Exception as e:
        return _error_response(e)
    finally:
        conn.close()


@app.route('/api/feedback', methods=['GET', 'POST'])
def feedback():
    """Pending/completed 14-day feedback or submit an outcome."""
    conn = dbm.connect(DB_PATH)
    try:
        repo = SQLiteRepository(conn)
        if request.method == 'GET':
            plant_id = request.args.get('plant_id')
            plant_id = int(plant_id) if plant_id else None
            if request.args.get('pending') in ('1', 'true', 'yes'):
                rows = repo.pending_feedback(plant_id=plant_id)
            else:
                rows = repo.all_feedback(plant_id=plant_id)
            for row in rows:
                row['feedback_state'] = svc.feedback_state(row)
            return jsonify(rows)
        data = request.get_json() or {}
        feedback_id = data.get('feedback_id')
        submitted = (data.get('feedback_submitted_date')
                     or data.get('date'))
        if feedback_id is None or not submitted:
            return jsonify({
                'error': 'feedback_id and feedback_submitted_date are '
                         'required',
            }), 400
        if 'survived_after_14d' not in data:
            return jsonify({'error': 'survived_after_14d is required'}), 400
        record = svc.submit_feedback(
            repo,
            feedback_id=int(feedback_id),
            feedback_submitted_date=submitted,
            survived_after_14d=bool(data.get('survived_after_14d')),
            height_after_14d_cm=data.get('height_after_14d_cm'),
            visible_condition=data.get('visible_condition'),
            user_notes=data.get('user_notes'),
        )
        return jsonify(record), 201
    except Exception as e:
        return _error_response(e)
    finally:
        conn.close()


@app.route('/api/predictions', methods=['GET'])
def prediction_history():
    """Append-only history of every prediction shown to the user."""
    species = _clean_date(request.args.get('species'))
    data_source = _clean_date(request.args.get('data_source'))
    scope = _clean_date(request.args.get('scope'))
    plant_id = request.args.get('plant_id')
    plant_id = int(plant_id) if plant_id else None
    limit = request.args.get('limit')
    limit = int(limit) if limit else None

    conn = dbm.connect(DB_PATH)
    try:
        repo = SQLiteRepository(conn)
        return jsonify(svc.list_prediction_history(
            repo, plant_id=plant_id, species=species,
            data_source=data_source, scope=scope, limit=limit))
    except Exception as e:
        return _error_response(e)
    finally:
        conn.close()


@app.route('/api/evaluation', methods=['GET'])
def evaluation():
    """Feedback-driven evaluation of predictions vs actual transplants."""
    species = _clean_date(request.args.get('species'))
    variety = _clean_variety(request.args.get('variety'))
    conn = dbm.connect(DB_PATH)
    try:
        repo = SQLiteRepository(conn)
        return jsonify(svc.evaluate_predictions(
            repo, species=species, variety=variety))
    except Exception as e:
        return _error_response(e)
    finally:
        conn.close()


if __name__ == '__main__':
    # Used by run.sh in development.
    app.run(host='0.0.0.0', port=8000)
