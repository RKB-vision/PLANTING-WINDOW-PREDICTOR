import { useEffect, useMemo, useState } from 'react'
import axios from 'axios'
import Link from 'next/link'
import { useRouter } from 'next/router'
import Layout from '../components/Layout'

interface CameraCapture {
  capture_id: number
  review_status: string
  public_id: string
  image_url: string | null
  observation_date: string
  capture_timestamp: string | null
  height_cm: number | null
  stem_width_cm: number | null
  visible_true_leaf_count: number | null
  confidence: number | null
  cm_per_pixel: number | null
  rod_length_cm: number | null
  calibration_source: string | null
  calibration_verified: boolean | number
  validity: string
  problem: string | null
  quality_flag?: string | null
}

interface CaptureGroup {
  plant_id: number
  external_ref: string | null
  species: string
  variety: string | null
  observation_date: string
  captures: CameraCapture[]
  proposed?: {
    median_height_cm?: number | null
    median_stem_width_cm?: number | null
    median_visible_true_leaf_count?: number | null
    n_valid?: number
    n_total?: number
    suspect?: boolean
    suspect_note?: string | null
  }
}

interface PredictionResult {
  available: boolean
  message: string
  method?: string
  provisional?: boolean
  days_remaining?: number
  predicted_readiness_date?: string | null
  days_remaining_uncertainty_days?: number | null
  age_source?: string
  estimate_basis?: string
  target_height_cm?: number | null
  robust_growth_rate_cm_per_day?: number | null
  n_history_readings?: number | null
  suspect_rows_excluded?: number
  date_stability?: string | null
  stability_note?: string | null
  cohort_snapshot?: {
    median_height_cm?: number | null
    median_growth_rate_cm_per_day?: number | null
    median_stem_width_cm?: number | null
    median_visible_true_leaf_count?: number | null
    median_age_days?: number | null
    observation_date?: string | null
    plants_represented?: number
  }
  limitations?: string[]
}

function queryString(value: string | string[] | undefined): string {
  return Array.isArray(value) ? value[0] || '' : value || ''
}

function formatValue(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

function isFutureDate(value: string | null | undefined): boolean {
  if (!value) return false
  return value > new Date().toISOString().slice(0, 10)
}

export default function VerifyReadings() {
  const router = useRouter()
  const species = queryString(router.query.species)
  const variety = queryString(router.query.variety)
  const observationDate = queryString(router.query.date)
  const workflowId = queryString(router.query.workflow)
  const [groups, setGroups] = useState<CaptureGroup[]>([])
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [acceptedCount, setAcceptedCount] = useState(0)
  const [prediction, setPrediction] = useState<PredictionResult | null>(null)
  const [outcome, setOutcome] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [predicting, setPredicting] = useState(false)
  const [activeIndex, setActiveIndex] = useState(0)

  const captures = useMemo(
    () => groups.flatMap(group => group.captures.filter(capture => capture.review_status === 'pending')),
    [groups]
  )
  const activeCapture = captures[Math.min(activeIndex, Math.max(0, captures.length - 1))]
  const activeGroup = activeCapture
    ? groups.find(group => group.captures.some(capture => capture.capture_id === activeCapture.capture_id))
    : undefined

  useEffect(() => {
    if (!router.isReady || !species) return

    let cancelled = false
    const loadPendingReadings = async () => {
      setLoading(true)
      setError(null)
      try {
        const storedAcceptedCount = workflowId
          ? Number(window.sessionStorage.getItem(`plant-review:${workflowId}`) || 0)
          : 0
        if (!cancelled) setAcceptedCount(storedAcceptedCount)
        const response = await axios.get('/api/captures', {
          params: { status: 'pending', species },
        })
        if (!cancelled) setGroups(response.data.groups || [])
      } catch (requestError: any) {
        if (!cancelled) {
          setError(requestError?.response?.data?.error || 'Could not load pending camera readings.')
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    loadPendingReadings()
    return () => { cancelled = true }
  }, [router.isReady, species, workflowId])

  const runPrediction = async () => {
    if (!species) return
    setPredicting(true)
    setError(null)
    setOutcome(null)
    try {
      const response = await axios.post('/api/predict/species', {
        species,
        variety: variety || null,
        observation_date: observationDate || null,
        refresh_camera: false,
      })
      setPrediction(response.data)
      if (workflowId) window.sessionStorage.removeItem(`plant-review:${workflowId}`)
    } catch (requestError: any) {
      setError(requestError?.response?.data?.error || 'Prediction failed after verification.')
    } finally {
      setPredicting(false)
    }
  }

  const toggleCapture = (captureId: number) => {
    setSelectedIds(previous => {
      const next = new Set(previous)
      if (next.has(captureId)) next.delete(captureId)
      else next.add(captureId)
      return next
    })
  }

  const toggleAll = () => {
    if (selectedIds.size === captures.length) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(captures.map(capture => capture.capture_id)))
    }
  }

  const review = async (action: 'accept' | 'discard') => {
    const pendingIds = captures.map(capture => capture.capture_id)
    const ids = selectedIds.size ? Array.from(selectedIds) : pendingIds
    if (!ids.length) return

    setSaving(true)
    setMessage(null)
    setError(null)
    try {
      const response = await axios.post('/api/captures/review', { ids, action })
      const reviewedCount = Number(response.data?.reviewed ?? ids.length)
      if (!reviewedCount) {
        setMessage('No pending readings were changed. Reload this page and try again.')
        return
      }

      const reviewedIds = new Set<number>((response.data?.capture_ids || ids).map(Number))
      const nextGroups = groups
        .map(group => ({
          ...group,
          captures: group.captures.filter(capture => !reviewedIds.has(capture.capture_id)),
        }))
        .filter(group => group.captures.length > 0)
      setGroups(nextGroups)
      setActiveIndex(index => Math.min(index, Math.max(0, captures.length - reviewedIds.size - 1)))
      setSelectedIds(new Set())

      const nextAcceptedCount = acceptedCount + (action === 'accept' ? reviewedCount : 0)
      if (action === 'accept') {
        setAcceptedCount(nextAcceptedCount)
        if (workflowId) {
          window.sessionStorage.setItem(`plant-review:${workflowId}`, String(nextAcceptedCount))
        }
      }

      const remainingCount = nextGroups.reduce(
        (count, group) => count + group.captures.filter(capture => capture.review_status === 'pending').length,
        0
      )
      if (remainingCount > 0) {
        setMessage(`${reviewedCount} reading(s) ${action === 'accept' ? 'verified' : 'discarded'}. ${remainingCount} still need review.`)
        return
      }

      if (nextAcceptedCount === 0) {
        setOutcome('All pending camera readings were discarded. No prediction was run, and existing observations were left unchanged.')
        if (workflowId) window.sessionStorage.removeItem(`plant-review:${workflowId}`)
        return
      }

      setMessage(`${reviewedCount} reading(s) ${action === 'accept' ? 'verified' : 'discarded'}.`)
      await runPrediction()
    } catch (requestError: any) {
      setError(requestError?.response?.data?.error || 'Could not save the review decision.')
    } finally {
      setSaving(false)
    }
  }

  const selectedCount = selectedIds.size
  const actionCount = selectedCount || captures.length
  const hasPending = captures.length > 0

  return (
    <Layout active="dashboard">
      <div className="page-head verification-page-head">
        <div>
          <p className="verification-eyebrow">Plant Readiness · Camera review</p>
          <h1 className="page-title">Verify OpenCV readings</h1>
          <p className="page-sub">Inspect each original image and its measured values before they enter the prediction data.</p>
        </div>
        <Link href="/" className="btn btn-secondary">Back to dashboard</Link>
      </div>

      <section className="verification-summary" aria-label="Review context">
        <div><span>Species</span><strong>{species || 'Loading…'}</strong></div>
        <div><span>Variety</span><strong>{variety && variety !== 'All varieties' ? variety : 'All varieties'}</strong></div>
        <div><span>Prediction date</span><strong>{observationDate || 'Latest available'}</strong></div>
        <div><span>Pending readings</span><strong>{loading ? '…' : captures.length}</strong></div>
      </section>

      {error && <div className="alert alert-danger" role="alert">{error}</div>}
      {message && <div className="alert alert-info" role="status">{message}</div>}
      {outcome && <div className="alert alert-warning" role="status">{outcome}</div>}

      {loading ? (
        <div className="verification-empty"><span className="spinner" /><span>Loading camera readings…</span></div>
      ) : hasPending ? (
        <>
          <div className="verification-toolbar">
            <label className="review-select verification-select-all">
              <input
                type="checkbox"
                checked={captures.length > 0 && selectedIds.size === captures.length}
                onChange={toggleAll}
              />
              Select all readings
            </label>
            <span className="muted small">
              {selectedCount ? `${selectedCount} selected of ${captures.length}` : 'With no selection, actions apply to all pending readings.'}
            </span>
          </div>

          {activeCapture && (
            <section className="verification-slideshow" aria-label="Camera reading slideshow">
              <div className="verification-slide-topbar">
                <div className="verification-group-heading">
                  <h2>{activeGroup?.external_ref || `Plant ${activeGroup?.plant_id}`}</h2>
                  <p>{activeGroup?.species}{activeGroup?.variety ? ` · ${activeGroup.variety}` : ''} · {activeGroup?.observation_date}
                    {isFutureDate(activeGroup?.observation_date) && (
                      <span className="badge badge-neutral" style={{ marginLeft: 8 }} title="Future-dated reading: shown for information, not excluded">Future-dated</span>
                    )}
                  </p>
                </div>
                <div className="verification-slide-navigation">
                  <button className="btn btn-secondary btn-sm" disabled={activeIndex === 0} onClick={() => setActiveIndex(index => Math.max(0, index - 1))} aria-label="Previous reading">← Previous</button>
                  <span aria-live="polite">{Math.min(activeIndex + 1, captures.length)} of {captures.length}</span>
                  <button className="btn btn-secondary btn-sm" disabled={activeIndex >= captures.length - 1} onClick={() => setActiveIndex(index => Math.min(captures.length - 1, index + 1))} aria-label="Next reading">Next →</button>
                </div>
              </div>
              <article className="verification-capture" key={activeCapture.capture_id}>
                    <div className="verification-image-column">
                      <div className="verification-image-frame">
                        {activeCapture.image_url ? (
                          <img className="verification-image" src={activeCapture.image_url} alt={`Original camera image ${activeCapture.public_id}`} />
                        ) : (
                          <p className="muted">Image preview unavailable</p>
                        )}
                      </div>
                      {activeCapture.image_url && (
                        <a href={activeCapture.image_url} target="_blank" rel="noreferrer" className="verification-image-link">
                          Open original image
                        </a>
                      )}
                      <p className="verification-public-id">{activeCapture.public_id}</p>
                    </div>

                    <div className="verification-data-column">
                      <div className="verification-data-heading">
                        <div>
                          <p className="verification-eyebrow">OpenCV measurement</p>
                          <h3>Reading details</h3>
                        </div>
                        <span className={`badge ${activeCapture.validity === 'valid' ? 'badge-success' : 'badge-warning'}`}>
                          {activeCapture.validity || 'unknown'}
                        </span>
                        {activeCapture.quality_flag === 'height_jump_suspect' && (
                          <span className="badge badge-warning" title="Differs by more than 50% from the previous accepted height">Height jump</span>
                        )}
                      </div>
                      {activeGroup?.proposed && (
                        <div className="small" style={{ margin: '4px 0 10px' }}>
                          <p style={{ margin: 0 }}>
                            Proposed daily median: <strong>{formatValue(activeGroup.proposed.median_height_cm)} cm</strong>
                            {activeGroup.proposed.n_valid != null && activeGroup.proposed.n_total != null
                              ? ` from ${activeGroup.proposed.n_valid} valid of ${activeGroup.proposed.n_total} image(s)`
                              : ''}
                          </p>
                          {activeGroup.proposed.suspect && (
                            <p className="alert alert-warning" style={{ marginTop: 6 }} role="status">
                              {activeGroup.proposed.suspect_note || 'This reading differs by more than 50% from the previous accepted height. It will be stored as suspect and excluded from growth rates and medians.'}
                            </p>
                          )}
                        </div>
                      )}
                      <dl className="verification-measurements">
                        <div><dt>Plant height</dt><dd>{formatValue(activeCapture.height_cm)} cm</dd></div>
                        <div><dt>Stem width</dt><dd>{activeCapture.stem_width_cm == null || activeCapture.stem_width_cm <= 0 ? 'not detected' : `${formatValue(activeCapture.stem_width_cm)} cm`}</dd></div>
                        <div><dt>Visible true leaves</dt><dd>{formatValue(activeCapture.visible_true_leaf_count)}</dd></div>
                        <div><dt>Image confidence</dt><dd>{activeCapture.confidence == null ? '—' : `${(activeCapture.confidence * 100).toFixed(1)}%`}</dd></div>
                        <div><dt>Scale</dt><dd>{activeCapture.cm_per_pixel == null ? '—' : `${activeCapture.cm_per_pixel} cm/px`}</dd></div>
                        <div><dt>Reference rod</dt><dd>{activeCapture.rod_length_cm == null ? '—' : `${activeCapture.rod_length_cm} cm`}</dd></div>
                        <div><dt>Calibration</dt><dd>{activeCapture.calibration_source || '—'}</dd></div>
                        <div><dt>Calibration verified</dt><dd>{activeCapture.calibration_verified ? 'Yes' : 'No'}</dd></div>
                        <div className="verification-measurement-wide"><dt>Captured at</dt><dd>{activeCapture.capture_timestamp || activeCapture.observation_date}</dd></div>
                      </dl>
                      {activeCapture.problem && <div className="verification-problem">{activeCapture.problem}</div>}
                      <label className="review-select verification-select-one">
                        <input
                          type="checkbox"
                          checked={selectedIds.has(activeCapture.capture_id)}
                          onChange={() => toggleCapture(activeCapture.capture_id)}
                        />
                        Include this reading in the action
                      </label>
                    </div>
              </article>
              <div className="verification-slide-dots" aria-label="Choose reading">
                {captures.map((capture, index) => (
                  <button key={capture.capture_id} className={`verification-slide-dot${index === activeIndex ? ' is-active' : ''}`} onClick={() => setActiveIndex(index)} aria-label={`Show reading ${index + 1}`} aria-current={index === activeIndex ? 'step' : undefined} />
                ))}
              </div>
            </section>
          )}

          <div className="verification-actions">
            <p className="muted small">Prediction runs only after at least one reading is verified and all pending readings have a decision.</p>
            <div className="review-actions">
              <button className="btn btn-danger-secondary" disabled={saving || predicting} onClick={() => review('discard')}>
                {saving ? 'Saving…' : selectedCount ? `Discard selected (${selectedCount})` : `Discard all pending (${actionCount})`}
              </button>
              <button className="btn" disabled={saving || predicting} onClick={() => review('accept')}>
                {saving || predicting ? 'Working…' : selectedCount ? `Verify selected (${selectedCount})` : `Verify all (${actionCount})`}
              </button>
            </div>
          </div>
        </>
      ) : (
        <section className="verification-empty">
          {prediction ? (
            <>
              <p className="verification-eyebrow">Prediction after verification</p>
              {prediction.available ? (
                <>
                  <h2>{prediction.days_remaining != null ? `${prediction.days_remaining} days remaining` : 'Prediction available'}</h2>
                  {prediction.predicted_readiness_date && <p>Estimated readiness date: <strong>{prediction.predicted_readiness_date}</strong></p>}
                  {prediction.provisional && <p className="muted small">Provisional historical baseline, not a validated model.</p>}
                  {prediction.estimate_basis === 'growth_trajectory' && (
                    <p className="muted small">
                      Projected from {formatValue(prediction.n_history_readings)} readings at{' '}
                      {formatValue(prediction.robust_growth_rate_cm_per_day)} cm/day toward the historical
                      transplant height of {formatValue(prediction.target_height_cm)} cm.
                    </p>
                  )}
                  {prediction.age_source === 'user_estimated_initial_age' && (
                    <p className="muted small">Plant age is a user-provided estimate, not a measured sowing date.</p>
                  )}
                  {(prediction.suspect_rows_excluded ?? 0) > 0 && (
                    <p className="muted small">{prediction.suspect_rows_excluded} suspect reading(s) excluded (&gt;50% height jump).</p>
                  )}
                  {prediction.stability_note && <p className="muted small">{prediction.stability_note}</p>}
                </>
              ) : <p>{prediction.message}</p>}
            </>
          ) : acceptedCount > 0 ? (
            <>
              <h2>{predicting ? 'Predicting from verified readings…' : 'Verified readings are ready'}</h2>
              <p className="muted">The accepted measurements are saved. Run prediction using those readings.</p>
              <button className="btn" disabled={predicting} onClick={runPrediction}>Run prediction</button>
            </>
          ) : (
            <>
              <h2>No pending camera readings</h2>
              <p className="muted">No new readings were found to verify. No prediction has been run from this page.</p>
            </>
          )}
          <Link href="/" className="btn btn-secondary">Return to dashboard</Link>
        </section>
      )}
    </Layout>
  )
}
