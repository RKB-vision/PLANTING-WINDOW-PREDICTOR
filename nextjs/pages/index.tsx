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
  level?: string
  mae_days?: number | null
  mae_days_std?: number | null
  baseline_mae_days?: number | null
  model_candidates?: Record<string, number> | null
  limitations?: string | null
  pending_review_count?: number
  status_note?: string | null
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
  if (result.provisional) return 'badge badge-info'
  if (result.available) return 'badge badge-success'
  return 'badge badge-neutral'
}

function methodLabel(result: PredictionResult): string {
  if (result.provisional) return 'Provisional baseline'
  if (result.available) return 'Validated model'
  return 'Insufficient evidence'
}

export default function Home() {
  const [speciesList, setSpeciesList] = useState<SpeciesOption[]>([])
  const [selectedSpecies, setSelectedSpecies] = useState('')
  const [selectedVariety, setSelectedVariety] = useState('All varieties')
  const [selectedDate, setSelectedDate] = useState('')
  const [loadingSpecies, setLoadingSpecies] = useState(true)
  const [loadingPrediction, setLoadingPrediction] = useState(false)
  const [result, setResult] = useState<PredictionResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showModal, setShowModal] = useState(false)
  const [modalStep, setModalStep] = useState<'loading' | 'review' | 'result'>('loading')
  const [pendingGroups, setPendingGroups] = useState<any[]>([])
  const [pendingCount, setPendingCount] = useState(0)
  const [reviewSelected, setReviewSelected] = useState<Set<number>>(new Set())
  const [reviewBusy, setReviewBusy] = useState(false)
  const [reviewMessage, setReviewMessage] = useState<string | null>(null)

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
    setShowModal(true)
    setModalStep('loading')
    setPendingGroups([])
    setReviewSelected(new Set())
    setReviewMessage(null)
    try {
      const resp = await axios.post('/api/predict/species', {
        species: selectedSpecies,
        variety: selectedVariety,
        observation_date: selectedDate || null,
      })
      setResult(resp.data)
      // Fetch staged readings for review.
      try {
        const capResp = await axios.get('/api/captures', {
          params: { status: 'pending', species: selectedSpecies },
        })
        const groups = capResp.data.groups || []
        setPendingGroups(groups)
        setPendingCount(capResp.data.pending_count || 0)
        const hasPending = groups.some((g: any) =>
          (g.captures || []).some((c: any) => c.review_status === 'pending')
        )
        setModalStep(hasPending ? 'review' : 'result')
      } catch {
        setModalStep('result')
      }
    } catch (e: any) {
      const msg = e?.response?.data?.error || 'Prediction request failed.'
      setError(msg)
      setModalStep('result')
    } finally {
      setLoadingPrediction(false)
    }
  }

  const toggleReview = (id: number) => {
    setReviewSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const reviewAction = async (action: 'accept' | 'discard') => {
    if (!reviewSelected.size) return
    setReviewBusy(true)
    setReviewMessage(null)
    try {
      await axios.post('/api/captures/review', {
        ids: Array.from(reviewSelected),
        action,
      })
      setReviewMessage(
        `${reviewSelected.size} reading(s) ${action === 'accept' ? 'accepted' : 'discarded'}.`
      )
      // Refresh pending list.
      const capResp = await axios.get('/api/captures', {
        params: { status: 'pending', species: selectedSpecies },
      })
      const groups = capResp.data.groups || []
      setPendingGroups(groups)
      setPendingCount(capResp.data.pending_count || 0)
      setReviewSelected(new Set())
      const hasPending = groups.some((g: any) =>
        (g.captures || []).some((c: any) => c.review_status === 'pending')
      )
      if (!hasPending) {
        // Re-run prediction now that readings are accepted.
        setModalStep('loading')
        try {
          const resp = await axios.post('/api/predict/species', {
            species: selectedSpecies,
            variety: selectedVariety,
            observation_date: selectedDate || null,
          })
          setResult(resp.data)
        } catch (e: any) {
          setError(e?.response?.data?.error || 'Prediction request failed.')
        }
        setModalStep('result')
      }
    } catch (e: any) {
      setReviewMessage(e?.response?.data?.error || 'Review failed.')
    } finally {
      setReviewBusy(false)
    }
  }

  const closeModal = () => {
    setShowModal(false)
    setModalStep('loading')
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

      {showModal && (
        <div
          className="modal-overlay"
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000, padding: 16 }}
          onClick={closeModal}
        >
          <div className="card" style={{ maxWidth: 720, width: '100%', maxHeight: '85vh', overflow: 'auto' }} onClick={e => e.stopPropagation()}>
            <div className="row-between" style={{ marginBottom: 12 }}>
              <h2 className="card-title" style={{ margin: 0 }}>Prediction</h2>
              <button className="btn btn-secondary btn-sm" onClick={closeModal}>Close</button>
            </div>

            {modalStep === 'loading' && <p className="muted">Working…</p>}

            {modalStep === 'review' && (
              <>
                <div className="alert alert-warning">
                  <p className="alert-title">Review pending readings</p>
                  <p>{pendingCount} new OpenCV reading(s) are staged and were not used. Accept or discard them; prediction will re-run.</p>
                </div>
                {reviewMessage && <p className="small">{reviewMessage}</p>}
                {pendingGroups.map((g: any) => (
                  <div key={`${g.plant_id}-${g.observation_date}`} className="card" style={{ marginBottom: 12, padding: 12 }}>
                    <p className="small" style={{ margin: 0, fontWeight: 600 }}>
                      {g.external_ref || `Plant ${g.plant_id}`} · {g.species} · {g.observation_date}
                    </p>
                    <div className="grid" style={{ marginTop: 8 }}>
                      {(g.captures || []).map((c: any) => (
                        c.review_status === 'pending' && (
                          <div key={c.capture_id} className="card" style={{ padding: 8 }}>
                            {c.image_url && (
                              <img src={c.image_url} alt={c.public_id} style={{ width: '100%', height: 120, objectFit: 'cover', borderRadius: 6 }} />
                            )}
                            <p className="small" style={{ margin: '4px 0 0' }}>
                              H {c.height_cm ?? '—'} cm · Stem {c.stem_width_cm ?? '—'} cm · Leaves {c.visible_true_leaf_count ?? '—'}
                            </p>
                            <label className="checkbox-row" style={{ marginTop: 4 }}>
                              <input type="checkbox" checked={reviewSelected.has(c.capture_id)} onChange={() => toggleReview(c.capture_id)} />
                              Select
                            </label>
                          </div>
                        )
                      ))}
                    </div>
                  </div>
                ))}
                <div className="btn-row" style={{ marginTop: 8 }}>
                  <button className="btn" disabled={reviewBusy || !reviewSelected.size} onClick={() => reviewAction('accept')}>Accept selected</button>
                  <button className="btn btn-secondary" disabled={reviewBusy || !reviewSelected.size} onClick={() => reviewAction('discard')}>Discard selected</button>
                </div>
              </>
            )}

            {modalStep === 'result' && result && (
              <>
                <div className={'answer ' + (result.available ? 'answer-ok' : 'answer-muted')}>
                  {result.available && result.days_remaining !== undefined ? (
                    <>
                      <div className="stat-label">{isPlantable(result) ? 'Transplant readiness' : 'Estimated days remaining'}</div>
                      <div className="hero-metric">{isPlantable(result) ? 'Plantable now' : `${result.days_remaining} days`}</div>
                      {result.predicted_readiness_date && (
                        <p className="answer-date">Readiness date: <strong>{result.predicted_readiness_date}</strong></p>
                      )}
                    </>
                  ) : (
                    <>
                      <div className="stat-label">Prediction unavailable</div>
                      <p style={{ margin: '8px 0 0' }}>{result.message}</p>
                    </>
                  )}
                  {result.provisional && (
                    <p className="small" style={{ margin: '10px 0 0' }}>Provisional estimate from the reference baseline.</p>
                  )}
                </div>

                {result.available && !result.provisional && result.mae_days != null && (
                  <div className="card" style={{ marginTop: 12, padding: 14 }}>
                    <p className="small" style={{ margin: 0, fontWeight: 600 }}>Model score (validation)</p>
                    <p className="small" style={{ margin: '4px 0 0' }}>
                      MAE {formatValue(result.mae_days)} days
                      {result.mae_days_std != null ? ` \u00b1 ${formatValue(result.mae_days_std)}` : ''}{' '}
                      \u00b7 baseline {formatValue(result.baseline_mae_days)} days (lower is better)
                    </p>
                    <p className="muted small" style={{ margin: '2px 0 0' }}>
                      {result.model_name ?? 'validated model'} \u00b7{' '}
                      {formatValue(result.n_independent_plants)} independent plants \u00b7{' '}
                      {formatValue(result.n_labelled_readiness_events)} readiness events
                    </p>
                    {result.model_candidates && Object.keys(result.model_candidates).length > 0 && (
                      <p className="muted small" style={{ margin: '2px 0 0' }}>
                        Candidates:{' '}
                        {Object.entries(result.model_candidates).map(([k, v]) => `${k} ${formatValue(v)} d`).join(' \u00b7 ')}
                      </p>
                    )}
                  </div>
                )}

                {result.provisional && (
                  <div className="card" style={{ marginTop: 12, padding: 14 }}>
                    <p className="small" style={{ margin: 0, fontWeight: 600 }}>Provisional baseline</p>
                    {result.days_remaining_uncertainty_days != null && result.days_remaining_uncertainty_days !== undefined && (
                      <p className="small" style={{ margin: '4px 0 0' }}>
                        Reference spread \u00b1{result.days_remaining_uncertainty_days} days
                      </p>
                    )}
                    <p className="muted small" style={{ margin: '2px 0 0' }}>
                      {formatValue(result.n_independent_plants)} independent plants \u00b7 transplant date
                      {result.reference_transplant_is_assumption ? ' assumed (not verified)' : ' recorded'}
                    </p>
                  </div>
                )}

                {cohort && (
                  <div className="card" style={{ marginTop: 12, padding: 14 }}>
                    <p className="small" style={{ margin: 0, fontWeight: 600 }}>
                      Values used (cohort medians, {formatValue(cohort.plants_represented)} plants)
                    </p>
                    <div className="grid" style={{ marginTop: 6 }}>
                      <p className="small" style={{ margin: 0 }}>Height: {formatValue(cohort.median_height_cm)} cm</p>
                      <p className="small" style={{ margin: 0 }}>Growth rate:{' '}
                        {cohort.median_growth_rate_cm_per_day != null
                          ? `${formatValue(cohort.median_growth_rate_cm_per_day)} cm/day`
                          : 'needs \u22652 measurements'}
                      </p>
                      <p className="small" style={{ margin: 0 }}>Canopy width: {formatValue(cohort.median_canopy_width_cm)} cm</p>
                      <p className="small" style={{ margin: 0 }}>Stem width: {formatValue(cohort.median_stem_width_cm)} cm</p>
                      <p className="small" style={{ margin: 0 }}>True leaves: {formatValue(cohort.median_visible_true_leaf_count)}</p>
                      <p className="small" style={{ margin: 0 }}>Age: {formatValue(cohort.median_age_days)} days</p>
                      <p className="small" style={{ margin: 0 }}>Observation date: {formatValue(cohort.observation_date)}</p>
                    </div>
                  </div>
                )}

                {result.limitations && (
                  <p className="muted small" style={{ margin: '8px 0 0' }}>Limitations: {result.limitations}</p>
                )}

                {!result.available && (
                  <p className="muted small" style={{ margin: '8px 0 0' }}>
                    Readings from the camera are stored even when prediction is unavailable.
                    Record a transplant in Feedback to create a training label.
                  </p>
                )}
              </>
            )}

            {modalStep === 'result' && !result && error && (
              <p className="muted">{error}</p>
            )}
          </div>
        </div>
      )}
    </Layout>
  )
}
