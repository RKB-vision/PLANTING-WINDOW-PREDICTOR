import { useEffect, useState } from 'react'
import axios from 'axios'
import Layout from '../components/Layout'
import Stat from '../components/Stat'

interface ReadinessCounts {
  independent_plants: number
  readiness_events: number
}

interface CohortSnapshot {
  aggregation: string
  plants_represented: number
  row_count: number
  observation_date: string | null
  median_height_cm: number | null
  min_height_cm: number | null
  max_height_cm: number | null
  median_age_days: number | null
  median_growth_rate_cm_per_day: number | null
  median_canopy_width_cm: number | null
  median_stem_width_cm: number | null
  median_visible_true_leaf_count: number | null
  missing_height_count?: number
  missing_age_count?: number
}

interface SpeciesOption {
  species: string
  plant_count: number
  observation_count: number
  latest_observation_date: string | null
  varieties: string[]
  available_dates: string[]
  readiness_event_counts: ReadinessCounts
}

interface PredictionResult {
  available: boolean
  message: string
  species: string
  variety: string | null
  observation_date: string | null
  synthetic: boolean
  synthetic_note?: string | null
  provisional?: boolean
  method?: string
  reference_basis?: string
  n_independent_plants?: number
  reference_transplant_is_assumption?: boolean
  insufficient_growth_history?: boolean
  history_note?: string | null
  days_remaining_uncertainty_days?: number | null
  days_remaining?: number
  past_readiness?: boolean
  readiness_status?: string | null
  predicted_readiness_date?: string | null
  level?: string
  mae_days?: number | null
  cohort_snapshot: CohortSnapshot
  readiness_event_counts: ReadinessCounts
}

function formatValue(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

/**
 * A countdown is only shown while the plant is still growing. Once readiness
 * has arrived (or already passed) the honest, non-alarming label is
 * "Plantable now" rather than a zero or negative number of days.
 */
function isPlantable(result: PredictionResult): boolean {
  if (!result.available) return false
  if (result.past_readiness) return true
  return typeof result.days_remaining === 'number' && result.days_remaining <= 0
}

function methodBadgeClass(result: PredictionResult): string {
  if (result.synthetic) return 'badge badge-warning'
  if (result.provisional) return 'badge badge-info'
  if (result.available) return 'badge badge-success'
  return 'badge badge-neutral'
}

function methodLabel(result: PredictionResult): string {
  if (result.synthetic) return 'Synthetic demo'
  if (result.provisional) return 'Provisional baseline'
  if (result.available) return 'Validated model'
  return 'Insufficient evidence'
}

export default function Home() {
  const [speciesList, setSpeciesList] = useState<SpeciesOption[]>([])
  const [selectedSpecies, setSelectedSpecies] = useState('')
  const [selectedVariety, setSelectedVariety] = useState('All varieties')
  const [selectedDate, setSelectedDate] = useState('')
  const [allowSynthetic, setAllowSynthetic] = useState(false)

  const [loadingSpecies, setLoadingSpecies] = useState(true)
  const [loadingPrediction, setLoadingPrediction] = useState(false)
  const [result, setResult] = useState<PredictionResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const fetchSpecies = async () => {
    setLoadingSpecies(true)
    setError(null)
    try {
      const resp = await axios.get('/api/species')
      const data: SpeciesOption[] = resp.data
      setSpeciesList(data)
      if (data.length && !selectedSpecies) {
        const preferred =
          data.find(item => item.latest_observation_date) || data[0]
        setSelectedSpecies(preferred.species)
      }
    } catch (e: any) {
      const msg = e?.response?.data?.error || 'Failed to load species catalog.'
      setError(msg)
    } finally {
      setLoadingSpecies(false)
    }
  }

  useEffect(() => {
    fetchSpecies()
  }, [])

  const currentSpecies = speciesList.find(s => s.species === selectedSpecies)

  useEffect(() => {
    if (!currentSpecies) return
    setSelectedVariety('All varieties')
    setSelectedDate(currentSpecies.latest_observation_date || '')
  }, [currentSpecies])

  const submitPrediction = async () => {
    if (!selectedSpecies) return
    setLoadingPrediction(true)
    setError(null)
    setResult(null)
    try {
      const resp = await axios.post('/api/predict/species', {
        species: selectedSpecies,
        variety: selectedVariety,
        observation_date: selectedDate || null,
        allow_synthetic: allowSynthetic,
      })
      setResult(resp.data)
    } catch (e: any) {
      const msg = e?.response?.data?.error || 'Prediction request failed.'
      setError(msg)
    } finally {
      setLoadingPrediction(false)
    }
  }

  const useLatestDate = () => {
    if (currentSpecies?.latest_observation_date) {
      setSelectedDate(currentSpecies.latest_observation_date)
    }
  }

  const cohort = result?.cohort_snapshot

  return (
    <Layout active="dashboard">
      <div className="page-head">
        <h1 className="page-title">Plant Readiness</h1>
        <p className="page-sub">
          Check whether a species is ready to transplant.
        </p>
      </div>

      <section className="card">
        <h2 className="card-title">Choose observation</h2>

        {loadingSpecies ? (
          <p className="muted">Loading species…</p>
        ) : speciesList.length === 0 ? (
          <div className="alert alert-warning">
            No species data has been imported yet.
          </div>
        ) : (
          <>
            <div className="grid-2">
              <div className="field">
                <label className="label" htmlFor="species">
                  Species
                </label>
                <select
                  id="species"
                  className="select"
                  value={selectedSpecies}
                  onChange={e => setSelectedSpecies(e.target.value)}
                >
                  {speciesList.map(item => (
                    <option key={item.species} value={item.species}>
                      {item.species}
                    </option>
                  ))}
                </select>
              </div>

              <div className="field">
                <label className="label" htmlFor="obs-date">
                  Observation date
                </label>
                <div className="inline-control">
                  <input
                    id="obs-date"
                    className="input"
                    type="date"
                    value={selectedDate}
                    onChange={e => setSelectedDate(e.target.value)}
                  />
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    onClick={useLatestDate}
                  >
                    Latest
                  </button>
                </div>
              </div>
            </div>

            {(currentSpecies?.varieties?.length ?? 0) > 1 && (
              <div className="field">
                <label className="label" htmlFor="variety">
                  Variety
                </label>
                <select
                  id="variety"
                  className="select"
                  value={selectedVariety}
                  onChange={e => setSelectedVariety(e.target.value)}
                >
                  <option value="All varieties">All varieties</option>
                  {currentSpecies?.varieties.map(v => (
                    <option key={v} value={v}>
                      {v}
                    </option>
                  ))}
                </select>
              </div>
            )}

            <div className="btn-row" style={{ marginTop: 4 }}>
              <button
                className="btn"
                onClick={submitPrediction}
                disabled={loadingPrediction}
              >
                {loadingPrediction ? (
                  <>
                    <span className="spinner" aria-hidden="true" />
                    Predicting…
                  </>
                ) : (
                  'Predict readiness'
                )}
              </button>
              <label className="checkbox-row" style={{ margin: 0 }}>
                <input
                  type="checkbox"
                  checked={allowSynthetic}
                  onChange={e => setAllowSynthetic(e.target.checked)}
                />
                Synthetic demo
              </label>
            </div>

            {currentSpecies && (
              <p className="muted small" style={{ margin: '16px 0 0' }}>
                {formatValue(currentSpecies.plant_count)} plants ·{' '}
                {formatValue(currentSpecies.observation_count)} observations ·
                latest {formatValue(currentSpecies.latest_observation_date)} ·{' '}
                {formatValue(
                  currentSpecies.readiness_event_counts?.readiness_events
                )}{' '}
                readiness events
              </p>
            )}
          </>
        )}
      </section>

      {error && (
        <div className="alert alert-danger">
          <p className="alert-title">Error</p>
          <p>{error}</p>
        </div>
      )}

      {result && (
        <section className="card">
          <div className="row-between" style={{ marginBottom: 16 }}>
            <h2 className="card-title" style={{ margin: 0 }}>
              Result
            </h2>
            <span className={methodBadgeClass(result)}>{methodLabel(result)}</span>
          </div>

          <div
            className={'answer ' + (result.available ? 'answer-ok' : 'answer-muted')}
          >
            {result.available && result.days_remaining !== undefined ? (
              <>
                <div className="stat-label">
                  {isPlantable(result)
                    ? 'Transplant readiness'
                    : 'Estimated days remaining'}
                </div>
                <div className="hero-metric">
                  {isPlantable(result) ? (
                    'Plantable now'
                  ) : (
                    <>
                      {result.days_remaining}{' '}
                      <span className="hero-unit">days</span>
                    </>
                  )}
                </div>
                {result.predicted_readiness_date && (
                  <p className="answer-date">
                    Readiness date:{' '}
                    <strong>{result.predicted_readiness_date}</strong>
                  </p>
                )}
              </>
            ) : (
              <>
                <div className="stat-label">Prediction unavailable</div>
                <p style={{ margin: '8px 0 0' }}>{result.message}</p>
              </>
            )}

            {result.synthetic && (
              <p className="small" style={{ margin: '10px 0 0' }}>
                Synthetic demo result — not a validated production prediction.
              </p>
            )}
            {result.provisional && !result.synthetic && (
              <p className="small" style={{ margin: '10px 0 0' }}>
                Provisional estimate from the reference baseline. The reference
                transplant date is assumed, not verified.
              </p>
            )}
          </div>

          <details className="disclosure">
            <summary>Details and evidence</summary>

            {result.reference_basis && (
              <p className="small" style={{ marginTop: 8 }}>
                Reference basis: {result.reference_basis} ·{' '}
                {formatValue(result.n_independent_plants)} independent plants
                {result.reference_transplant_is_assumption
                  ? ' (transplant date assumed)'
                  : ''}
              </p>
            )}

            {result.provisional &&
              result.days_remaining_uncertainty_days !== null &&
              result.days_remaining_uncertainty_days !== undefined && (
                <p className="small">
                  Uncertainty ±{result.days_remaining_uncertainty_days} days
                  (reference-group spread)
                </p>
              )}

            {result.history_note && (
              <p className="small">{result.history_note}</p>
            )}



            {cohort && (
              <p className="small">
                Cohort: {formatValue(cohort.median_height_cm)} cm height ·{' '}
                {formatValue(cohort.median_age_days)} days ·{' '}
                {formatValue(cohort.median_growth_rate_cm_per_day)} cm/day ·{' '}
                {formatValue(cohort.plants_represented)} plants
              </p>
            )}



            <p className="muted small" style={{ margin: '8px 0 0' }}>
              {formatValue(result.readiness_event_counts?.independent_plants)}{' '}
              independent plants ·{' '}
              {formatValue(result.readiness_event_counts?.readiness_events)}{' '}
              readiness events
            </p>
          </details>
        </section>
      )}
    </Layout>
  )
}
