import { useEffect, useState } from 'react'
import axios from 'axios'
import { useRouter } from 'next/router'
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
  age_source?: string
  rows_total?: number
  suspect_rows_excluded?: number
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

interface CameraRefreshReport {
  enabled: boolean
  ran: boolean
  reason: string | null
  errors: string[]
  captures_staged?: number
  sources: Array<{
    prefix: string
    external_ref: string
    status: string
    message: string | null
    error_step?: string
    error_type?: string
  }>
}

interface PredictionResult {
  available: boolean
  message: string
  model_warning?: string
  species: string
  variety: string | null
  observation_date: string | null

  provisional?: boolean
  method?: string
  model_name?: string | null
  reference_basis?: string
  n_independent_plants?: number
  n_labelled_readiness_events?: number
  reference_transplant_is_assumption?: boolean
  insufficient_growth_history?: boolean
  history_note?: string | null
  days_remaining_uncertainty_days?: number | null
  days_remaining?: number
  past_readiness?: boolean
  readiness_status?: string | null
  predicted_readiness_date?: string | null
  age_source?: string
  estimate_basis?: string
  target_height_cm?: number | null
  current_height_cm?: number | null
  remaining_growth_cm?: number | null
  robust_growth_rate_cm_per_day?: number | null
  n_history_readings?: number | null
  history_span_days?: number | null
  horizon_capped?: boolean
  suspect_rows_excluded?: number
  date_stability?: string | null
  date_capped_to_previous?: boolean
  previous_predicted_readiness_date?: string | null
  plant_grew_since_previous?: boolean
  stability_note?: string | null
  level?: string
  mae_days?: number | null
  mae_days_std?: number | null
  baseline_mae_days?: number | null
  model_candidates?: Record<string, number> | null
  limitations?: string | null
  pending_review_count?: number
  status_note?: string | null
  camera_refresh?: CameraRefreshReport
  cohort_snapshot: CohortSnapshot
  readiness_event_counts: ReadinessCounts
}

function formatValue(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

/**
 * Shows what happened when the app checked Cloudinary for new camera images.
 *
 * A camera/network failure is reported separately from "prediction
 * unavailable": the refresh is fail-open, so stored readings are still used.
 * Only the first failure is shown, because repeating the same message for
 * every source reads like several unrelated errors.
 */
function CameraRefreshNotice({ report }: { report: CameraRefreshReport }) {
  if (!report.enabled) {
    return (
      <p className="muted small" style={{ margin: '12px 0 0' }}>
        Camera check is off{report.reason ? `: ${report.reason}` : '.'}
      </p>
    )
  }

  const failures = report.sources.filter(
    source => source.status === 'error' || source.status === 'import_error'
  )

  if (failures.length > 0) {
    const first = failures[0]
    const connectionProblem = /^(OSError|URLError|TimeoutError|ConnectionError|HTTPException)$/.test(
      first.error_type || ''
    )
    return (
      <div className="alert alert-warning" role="status" style={{ marginTop: 12 }}>
        <p className="alert-title">Couldn&apos;t check for new camera images</p>
        <p className="small" style={{ margin: 0 }}>
          {connectionProblem
            ? 'The camera service could not be reached.'
            : 'A new camera reading could not be checked.'}
        </p>
        <p className="muted small" style={{ margin: '4px 0 0' }}>
          {first.error_step === 'import_measurements'
            ? 'The image was measured, but the reading could not be saved.'
            : 'The prediction used readings that were already saved. This is usually temporary; retry in a moment.'}
        </p>
        <details className="muted small" style={{ marginTop: 6 }}>
          <summary>Technical detail</summary>
          <p style={{ margin: '4px 0 0' }}>
            {[first.error_type, first.message].filter(Boolean).join(': ') || first.status}
          </p>
        </details>
      </div>
    )
  }

  const staged = report.captures_staged ?? 0
  return (
    <p className="muted small" style={{ margin: '12px 0 0' }}>
      {staged > 0
        ? `${staged} new camera reading(s) were found and are waiting for verification.`
        : 'No new camera images were found.'}
    </p>
  )
}

function isPlantable(result: PredictionResult): boolean {
  if (!result.available) return false
  if (result.past_readiness) return true
  return typeof result.days_remaining === 'number' && result.days_remaining <= 0
}

function methodBadgeClass(result: PredictionResult): string {
  if (result.provisional) return 'badge badge-info'
  if (result.available) return 'badge badge-success'
  return 'badge badge-neutral'
}

function methodLabel(result: PredictionResult): string {
  if (result.provisional) {
    return result.estimate_basis === 'growth_trajectory'
      ? 'Trend projection'
      : 'Historical timing'
  }
  if (result.available) return 'Validated model'
  return 'Insufficient evidence'
}

function ageSourceLabel(source?: string): string {
  if (source === 'planted_on') return 'from planting date'
  if (source === 'user_estimated_initial_age') return 'user-provided estimate'
  if (source === 'mixed') return 'mixed sources across plants'
  return 'not recorded'
}

export default function Home() {
  const router = useRouter()
  const [speciesList, setSpeciesList] = useState<SpeciesOption[]>([])
  const [selectedSpecies, setSelectedSpecies] = useState('')
  const [selectedVariety, setSelectedVariety] = useState('All varieties')
  const [selectedDate, setSelectedDate] = useState('')
  const [loadingSpecies, setLoadingSpecies] = useState(true)
  const [loadingPrediction, setLoadingPrediction] = useState(false)
  const [result, setResult] = useState<PredictionResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showResultModal, setShowResultModal] = useState(false)

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
    setShowResultModal(false)
    try {
      // Refresh and stage camera readings first. The prediction endpoint is
      // intentionally called only after the user has accepted/discarded every
      // pending reading for this species.
      const refreshResp = await axios.post('/api/camera/refresh', {
        species: selectedSpecies,
      })
      const refreshReport: CameraRefreshReport = refreshResp.data
      const capResp = await axios.get('/api/captures', {
        params: { status: 'pending', species: selectedSpecies },
      })
      const groups = capResp.data.groups || []
      const hasPending = groups.some((group: any) =>
        (group.captures || []).some((capture: any) => capture.review_status === 'pending')
      )
      if (hasPending) {
        const workflowId = String(Date.now())
        window.sessionStorage.setItem(`plant-review:${workflowId}`, '0')
        await router.push({
          pathname: '/verify-readings',
          query: {
            species: selectedSpecies,
            variety: selectedVariety,
            date: selectedDate,
            workflow: workflowId,
          },
        })
        return
      }

      const resp = await axios.post('/api/predict/species', {
        species: selectedSpecies,
        variety: selectedVariety,
        observation_date: selectedDate || null,
        refresh_camera: false,
      })
      setResult({ ...resp.data, camera_refresh: refreshReport })
      setShowResultModal(true)
    } catch (e: any) {
      const msg = e?.response?.data?.error || 'Could not refresh camera readings.'
      setError(msg)
      setResult(null)
      setShowResultModal(true)
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
        ) : error && speciesList.length === 0 ? (
          <div className="alert alert-danger" role="alert">
            <p className="alert-title">Could not load the species catalog</p>
            <p>{error}</p>
            <p>Check that the Flask backend is running, then reload this page.</p>
          </div>
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

      {error && speciesList.length > 0 && (
        <div className="alert alert-danger">
          <p className="alert-title">Error</p>
          <p>{error}</p>
        </div>
      )}

      {showResultModal && (
        <div className="modal-overlay" style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000, padding: 16 }} onClick={() => setShowResultModal(false)}>
          <div className="card" style={{ maxWidth: 720, width: '100%', maxHeight: '85vh', overflow: 'auto' }} onClick={e => e.stopPropagation()}>
            <div className="row-between" style={{ marginBottom: 12 }}>
              <h2 className="card-title" style={{ margin: 0 }}>Prediction result</h2>
              <button className="btn btn-secondary btn-sm" onClick={() => setShowResultModal(false)}>Close</button>
            </div>

            {!result && error && (
              <div className="alert alert-danger" role="alert">{error}</div>
            )}
            {result && (
              <>
                <div className={'answer ' + (result.available ? 'answer-ok' : 'answer-muted')}>
                  {result.available && result.days_remaining !== undefined ? (
                    <>
                      <div className="stat-label">{isPlantable(result) ? 'Transplant readiness' : 'Estimated days remaining'}</div>
                      <div className="hero-metric">{isPlantable(result) ? 'Plantable now' : `${Math.ceil(result.days_remaining)} days`}</div>
                      {!isPlantable(result) && (
                        <div className="answer-precision">Exact estimate: {result.days_remaining} days</div>
                      )}
                      {result.predicted_readiness_date && (
                        <p className="answer-date">Readiness date: <strong>{result.predicted_readiness_date}</strong></p>
                      )}
                      {result.provisional && (
                        <p className="small" style={{ margin: '4px 0 0' }}>
                          <span className={methodBadgeClass(result)}>{methodLabel(result)}</span>
                        </p>
                      )}
                    </>
                  ) : (
                    <>
                      <div className="stat-label">Prediction unavailable</div>
                      <p style={{ margin: '8px 0 0' }}>{result.message}</p>
                    </>
                  )}
                  {result.provisional && (
                    <p className="small" style={{ margin: '10px 0 0' }}>Provisional estimate from the reference baseline, not a validated model.</p>
                  )}
                  {result.date_capped_to_previous && (
                    <p className="small" style={{ margin: '6px 0 0', color: 'var(--muted, #5b6b5b)' }}>
                      The plant grew since the previous reading, so the date was held at{' '}
                      {result.previous_predicted_readiness_date || 'the previous estimate'} instead of moving later.
                    </p>
                  )}
                  {result.date_stability === 'later_after_stall' && (
                    <p className="small" style={{ margin: '6px 0 0' }}>
                      Height did not increase since the previous reading, so a later date is allowed.
                    </p>
                  )}
                  {result.date_stability === 'method_changed' && result.stability_note && (
                    <p className="small" style={{ margin: '6px 0 0' }}>{result.stability_note}</p>
                  )}
                  {result.model_warning && (
                    <p className="small muted" role="status" style={{ margin: '6px 0 0' }}>
                      {result.model_warning}
                    </p>
                  )}
                </div>

                {result.available && !result.provisional && result.mae_days != null && (
                  <div className="card model-validation" aria-label="Model validation results">
                    <div className="model-validation-heading">
                      <div>
                        <p className="verification-eyebrow">How this estimate performed</p>
                        <h3>Validation summary</h3>
                      </div>
                      <span className="badge badge-success">Validated</span>
                    </div>
                    <div className="model-validation-stats">
                      <div className="model-validation-stat model-validation-primary">
                        <span>Average prediction error</span>
                        <strong>{formatValue(result.mae_days)} days</strong>
                        {result.mae_days_std != null && <small>Variation across tests: ±{formatValue(result.mae_days_std)} days</small>}
                      </div>
                      <div className="model-validation-stat">
                        <span>Simple baseline error</span>
                        <strong>{formatValue(result.baseline_mae_days)} days</strong>
                        <small>Lower error is better</small>
                      </div>
                    </div>
                    <p className="model-validation-evidence">
                      <strong>{String(result.model_name ?? 'Validated model').replaceAll('_', ' ')}</strong>
                      <span>{formatValue(result.n_independent_plants)} plants</span>
                      <span>{formatValue(result.n_labelled_readiness_events)} readiness records</span>
                    </p>
                    {result.model_candidates && Object.keys(result.model_candidates).length > 0 && (
                      <div className="model-candidate-list">
                        <span className="model-candidate-title">Compared models</span>
                        <div>
                          {Object.entries(result.model_candidates).map(([name, value]) => (
                            <span className="model-candidate" key={name}>
                              <span>{name.replaceAll('_', ' ')}</span>
                              <strong>{formatValue(value)} d</strong>
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                )}

                {cohort && (
                  <div className="card" style={{ marginTop: 12, padding: 14 }}>
                    <p className="small" style={{ margin: 0, fontWeight: 600 }}>Values used</p>
                    <div className="grid" style={{ marginTop: 6 }}>
                      <p className="small" style={{ margin: 0 }}>Height: {formatValue(cohort.median_height_cm)} cm</p>
                      <p className="small" style={{ margin: 0 }}>Growth rate:{' '}
                        {cohort.median_growth_rate_cm_per_day != null
                          ? `${formatValue(cohort.median_growth_rate_cm_per_day)} cm/day`
                          : 'not available; needs measurements on 2 different dates for the same plant'}
                      </p>
                      <p className="small" style={{ margin: 0 }}>Stem width: {cohort.median_stem_width_cm != null && cohort.median_stem_width_cm > 0 ? `${formatValue(cohort.median_stem_width_cm)} cm` : 'not measured'}</p>
                      <p className="small" style={{ margin: 0 }}>True leaves: {formatValue(cohort.median_visible_true_leaf_count)}</p>
                      <p className="small" style={{ margin: 0 }}>Age: {cohort.median_age_days == null ? 'not recorded' : `${cohort.median_age_days} days`}{cohort.median_age_days != null ? ` (${ageSourceLabel(result.age_source || cohort.age_source)})` : ''}</p>
                      <p className="small" style={{ margin: 0 }}>Observation date: {formatValue(cohort.observation_date)}</p>
                      {result.estimate_basis === 'growth_trajectory' && (
                        <>
                          <p className="small" style={{ margin: 0 }}>Target height: {formatValue(result.target_height_cm)} cm (historical transplant median)</p>
                          <p className="small" style={{ margin: 0 }}>Projected growth: {result.robust_growth_rate_cm_per_day != null ? `${formatValue(result.robust_growth_rate_cm_per_day)} cm/day` : 'not available'}</p>
                          <p className="small" style={{ margin: 0 }}>Readings used: {formatValue(result.n_history_readings)}{result.history_span_days != null ? ` over ${result.history_span_days} days` : ''}</p>
                        </>
                      )}
                      {(result.suspect_rows_excluded ?? cohort.suspect_rows_excluded ?? 0) > 0 && (
                        <p className="small" style={{ margin: 0 }}>
                          Suspect readings excluded: {result.suspect_rows_excluded ?? cohort.suspect_rows_excluded} (&gt;50% height jump; kept for review)
                        </p>
                      )}
                    </div>
                  </div>
                )}

                {!result.available && (
                  <>
                    {result.camera_refresh && (
                      <CameraRefreshNotice report={result.camera_refresh} />
                    )}
                    <p className="muted small" style={{ margin: '8px 0 0' }}>
                      Verified camera readings are saved even when a prediction
                      is not possible yet. Record a transplant in Feedback to
                      create a training label.
                    </p>
                  </>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </Layout>
  )
}
