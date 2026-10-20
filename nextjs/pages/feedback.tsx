import { useEffect, useState } from 'react'
import axios from 'axios'
import Layout from '../components/Layout'
import Stat from '../components/Stat'

interface Plant {
  plant_id: number
  species: string
  variety: string | null
  external_ref: string | null
  cohort?: string | null
  data_source: string
}

interface Prediction {
  prediction_id: number
  plant_id: number | null
  species: string
  variety: string | null
  method: string
  provisional: boolean
  available: boolean
  days_remaining: number | null
  predicted_readiness_date: string | null
  created_at: string
  message: string | null
}

interface FeedbackRow {
  feedback_id: number
  plant_id: number
  transplant_date: string
  feedback_due_date: string
  species?: string
  variety?: string | null
  transplant_occurred?: boolean
  condition_at_transplant?: string | null
  transplant_notes?: string | null
  feedback_state?: { status: string; days_overdue: number }
}

interface Evaluation {
  n_predictions_evaluated: number
  n_independent_plants: number
  skipped_after_transplant: number
  sufficient_evidence: boolean
  overall: {
    mean_abs_error_days: number | null
    match_rate: number | null
    n_with_14d_outcome: number
    survival_rate_14d: number | null
    n_incomplete_feedback: number
  }
  by_method: Record<
    string,
    {
      n_predictions: number
      n_independent_plants: number
      mean_abs_error_days: number | null
    }
  >
  comparison: {
    methods_compared: string[]
    better_method: string | null
    auto_applied: boolean
    recommendation: string
  }
  notes: string[]
}

interface Baseline {
  species: string
  n_plants: number
  n_points: number
  n_plants_contributing_to_medians: number
  transplant_reference?: {
    assumption: boolean
    n_recorded: number
    n_assumed: number
    rule: string
  }
  by_age_days: {
    age_days: number
    n_plants: number
    median_height_cm: number | null
    median_days_until_transplant: number | null
  }[]
  limitations: string[]
}

type TabKey = 'feedback' | 'reference' | 'evaluation'

const TABS: { key: TabKey; label: string }[] = [
  { key: 'feedback', label: 'Feedback' },
  { key: 'reference', label: 'Reference' },
  { key: 'evaluation', label: 'Evaluation' },
]

function fmt(v: number | string | null | undefined, suffix = ''): string {
  if (v === null || v === undefined || v === '') return '—'
  return `${v}${suffix}`
}

/** Show an arrived/passed countdown as "Plantable now", never as a negative. */
function countdownText(days: number | null | undefined): string {
  if (days === null || days === undefined) return '—'
  return days <= 0 ? 'Plantable now' : `${days} days`
}

export default function FeedbackPage() {
  const [plants, setPlants] = useState<Plant[]>([])
  const [species, setSpecies] = useState<string[]>([])
  const [selectedSpecies, setSelectedSpecies] = useState('')
  const [baseline, setBaseline] = useState<Baseline | null>(null)
  const [baselineMissing, setBaselineMissing] = useState(false)

  const [predictions, setPredictions] = useState<Prediction[]>([])
  const [pending, setPending] = useState<FeedbackRow[]>([])
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<TabKey>('feedback')

  // dataset upload (Feedback -> Reference tab)
  const [uploadCsv, setUploadCsv] = useState('')
  const [uploadSpecies, setUploadSpecies] = useState('')
  const [uploadBusy, setUploadBusy] = useState(false)
  const [uploadResult, setUploadResult] = useState<string | null>(null)

  // assessment form
  const [assessPlant, setAssessPlant] = useState('')
  const [assessDate, setAssessDate] = useState('')
  const [assessStatus, setAssessStatus] = useState('uncertain')
  const [assessNotes, setAssessNotes] = useState('')
  const [assessPrediction, setAssessPrediction] = useState('')

  // transplant form
  const [txPlant, setTxPlant] = useState('')
  const [txDate, setTxDate] = useState('')
  const [txHeight, setTxHeight] = useState('')
  const [txCondition, setTxCondition] = useState('')
  const [txOccurred, setTxOccurred] = useState(true)
  const [txNotes, setTxNotes] = useState('')

  // follow-up form
  const [followFeedback, setFollowFeedback] = useState('')
  const [followDate, setFollowDate] = useState('')
  const [followSurvived, setFollowSurvived] = useState(true)
  const [followHeight, setFollowHeight] = useState('')
  const [followCondition, setFollowCondition] = useState('healthy')
  const [followNotes, setFollowNotes] = useState('')

  const loadPlants = async () => {
    try {
      const resp = await axios.get('/api/plants')
      const data: Plant[] = resp.data
      // Reference-cohort plants (imported from the historical CSV) exist only
      // to build the species baseline. Operational steps act on monitored
      // plants, so the cohort is deliberately hidden here.
      const live = data.filter(
        p => p.data_source === 'real' && p.cohort !== 'reference'
      )
      setPlants(live)
      const uniqueSpecies = Array.from(new Set(data.map(p => p.species)))
      setSpecies(uniqueSpecies)
      if (live.length) {
        // Focus the feedback flow on the monitored plant.
        setSelectedSpecies(prev => prev || live[0].species)
        setAssessPlant(prev => prev || String(live[0].plant_id))
        setTxPlant(prev => prev || String(live[0].plant_id))
      } else if (uniqueSpecies.length && !selectedSpecies) {
        setSelectedSpecies(uniqueSpecies[0])
      }
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to load plants.')
    }
  }

  const loadPending = async () => {
    try {
      const resp = await axios.get('/api/feedback', { params: { pending: 1 } })
      setPending(resp.data)
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to load pending feedback.')
    }
  }

  const loadPredictions = async (plantId?: number) => {
    try {
      const resp = await axios.get('/api/predictions', {
        params: plantId ? { plant_id: plantId } : { limit: 20 },
      })
      setPredictions(resp.data)
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to load predictions.')
    }
  }

  const loadEvaluation = async (sp?: string) => {
    try {
      const resp = await axios.get('/api/evaluation', {
        params: sp ? { species: sp } : {},
      })
      setEvaluation(resp.data)
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to load evaluation.')
    }
  }

  const loadBaseline = async (sp: string) => {
    if (!sp) return
    setBaseline(null)
    setBaselineMissing(false)
    try {
      const resp = await axios.get('/api/reference', { params: { species: sp } })
      setBaseline(resp.data)
    } catch (e: any) {
      if (e?.response?.status === 404) {
        setBaselineMissing(true)
      } else {
        setError(e?.response?.data?.error || 'Failed to load reference baseline.')
      }
    }
  }

  useEffect(() => {
    loadPlants()
    loadPending()
    loadPredictions()
    loadEvaluation()
  }, [])

  useEffect(() => {
    if (selectedSpecies) {
      loadBaseline(selectedSpecies)
      loadEvaluation(selectedSpecies)
    }
  }, [selectedSpecies])

  const buildBaseline = async () => {
    setError(null)
    try {
      const resp = await axios.post('/api/reference', {
        species: selectedSpecies,
      })
      setBaseline(resp.data)
      setBaselineMissing(false)
      setMessage('Reference baseline built from stored measurements.')
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to build baseline.')
    }
  }

  const submitUpload = async () => {
    setError(null)
    setUploadResult(null)
    if (!uploadCsv.trim()) {
      setError('Paste a CSV (with at least Date, Age/ Age_days, Plant_ID, Height_cm).')
      return
    }
    const sp = (uploadSpecies || selectedSpecies).trim()
    if (!sp) {
      setError('Species is required.')
      return
    }
    setUploadBusy(true)
    try {
      const resp = await axios.post('/api/import', {
        csv_text: uploadCsv,
        species: sp,
      })
      const data = resp.data
      setUploadResult(
        `Imported ${data.observations_inserted} new observation(s) ` +
        `(${data.observations_skipped_duplicate ?? 0} already present) ` +
        `across ${(data.preview?.plants_in_file ?? []).length} plant(s) ` +
        `in the file. Baseline ` +
        (data.baseline ? 'built.' : 'not built.')
      )
      if (sp === selectedSpecies) {
        loadBaseline(sp)
      }
      // refresh species list
      loadPlants()
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Upload failed.')
      const issues = e?.response?.data?.issues
      if (Array.isArray(issues) && issues.length) {
        setError(
          (e?.response?.data?.error || 'Upload failed.') +
          ' Issues: ' +
          issues.map((i: any) => `line ${i.line}: ${i.problem}`).join('; ')
        )
      }
    } finally {
      setUploadBusy(false)
    }
  }

  const submitAssessment = async () => {
    setError(null)
    setMessage(null)
    if (!assessDate) {
      setError('Assessment date is required.')
      return
    }
    try {
      await axios.post('/api/assessments', {
        plant_id: assessPlant ? Number(assessPlant) : null,
        species: selectedSpecies,
        assessment_date: assessDate,
        readiness_status: assessStatus,
        notes: assessNotes || null,
        prediction_id: assessPrediction ? Number(assessPrediction) : null,
      })
      setMessage('Readiness assessment recorded. This does not record a transplant.')
      setAssessNotes('')
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to record assessment.')
    }
  }

  const submitTransplant = async () => {
    setError(null)
    setMessage(null)
    if (!txPlant || !txDate) {
      setError('Plant and transplant date are required.')
      return
    }
    try {
      await axios.post('/api/transplant', {
        plant_id: Number(txPlant),
        transplant_date: txDate,
        height_at_transplant_cm: txHeight ? Number(txHeight) : null,
        condition_at_transplant: txCondition || null,
        transplant_occurred: txOccurred,
        transplant_notes: txNotes || null,
      })
      setMessage(
        txOccurred
          ? 'Transplant recorded. A 14-day follow-up is now due.'
          : 'Transplant decision recorded as not occurred (no follow-up due).'
      )
      setTxNotes('')
      loadPending()
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to record transplant.')
    }
  }

  const submitFollowUp = async () => {
    setError(null)
    setMessage(null)
    if (!followFeedback || !followDate) {
      setError('Select a feedback record and a submission date.')
      return
    }
    try {
      await axios.post('/api/feedback', {
        feedback_id: Number(followFeedback),
        feedback_submitted_date: followDate,
        survived_after_14d: followSurvived,
        height_after_14d_cm: followHeight ? Number(followHeight) : null,
        visible_condition: followCondition || null,
        user_notes: followNotes || null,
      })
      setMessage('14-day follow-up recorded.')
      setFollowFeedback('')
      setFollowNotes('')
      loadPending()
      loadEvaluation(selectedSpecies)
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to submit follow-up.')
    }
  }

  const plantLabel = (p: Plant) =>
    `#${p.plant_id} ${p.species}${p.variety ? ` / ${p.variety}` : ''}${
      p.external_ref ? ` (${p.external_ref})` : ''
    }`

  return (
    <Layout active="feedback">
      <div className="page-head row-between">
        <div>
          <h1 className="page-title">Feedback</h1>
          <p className="page-sub">Record feedback and review predictions.</p>
        </div>
        <div className="field" style={{ margin: 0, minWidth: 220 }}>
          <label className="label" htmlFor="species">
            Species
          </label>
          <select
            id="species"
            className="select"
            value={selectedSpecies}
            onChange={e => setSelectedSpecies(e.target.value)}
          >
            {species.map(s => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
      </div>

      {message && <div className="alert alert-success">{message}</div>}
      {error && (
        <div className="alert alert-danger">
          <p className="alert-title">Error</p>
          <p>{error}</p>
        </div>
      )}

      <div className="tabs" role="tablist">
        {TABS.map(t => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            className={'tab' + (tab === t.key ? ' is-active' : '')}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'feedback' && (
        <>
          {plants.length === 0 && (
            <p className="muted small">
              No monitored plant yet — reference-cohort plants are hidden from
              these steps. Register the camera plant (for example{' '}
              <code>plant_feedback.py register --ref Plant-01</code>) to record
              assessments and transplants for it.
            </p>
          )}
          <section className="card">
            <h2 className="card-title step-title">
              <span className="step">1</span>Readiness assessment
            </h2>
            <p className="card-hint">
              Your judgement — separate from the system prediction.
            </p>
            <div className="grid-2">
              <div className="field">
                <label className="label" htmlFor="assess-plant">
                  Plant
                </label>
                <select
                  id="assess-plant"
                  className="select"
                  value={assessPlant}
                  onChange={e => {
                    setAssessPlant(e.target.value)
                    loadPredictions(Number(e.target.value))
                  }}
                >
                  <option value="">(none — species-level)</option>
                  {plants
                    .filter(p => p.species === selectedSpecies)
                    .map(p => (
                      <option key={p.plant_id} value={p.plant_id}>
                        {plantLabel(p)}
                      </option>
                    ))}
                </select>
              </div>
              <div className="field">
                <label className="label" htmlFor="assess-date">
                  Date
                </label>
                <input
                  id="assess-date"
                  className="input"
                  type="date"
                  value={assessDate}
                  onChange={e => setAssessDate(e.target.value)}
                />
              </div>
              <div className="field">
                <label className="label" htmlFor="assess-status">
                  Readiness
                </label>
                <select
                  id="assess-status"
                  className="select"
                  value={assessStatus}
                  onChange={e => setAssessStatus(e.target.value)}
                >
                  <option value="ready">ready</option>
                  <option value="not_ready">not_ready</option>
                  <option value="uncertain">uncertain</option>
                </select>
              </div>
            </div>
            <details className="disclosure">
              <summary>More options</summary>
              <div className="field" style={{ marginTop: 10 }}>
                <label className="label" htmlFor="assess-pred">
                  Prediction shown (optional)
                </label>
                <select
                  id="assess-pred"
                  className="select"
                  value={assessPrediction}
                  onChange={e => setAssessPrediction(e.target.value)}
                >
                  <option value="">(none)</option>
                  {predictions.map(p => (
                    <option key={p.prediction_id} value={p.prediction_id}>
                      #{p.prediction_id} {p.method}{' '}
                      {p.predicted_readiness_date || ''}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label className="label" htmlFor="assess-notes">
                  Notes
                </label>
                <textarea
                  id="assess-notes"
                  className="textarea"
                  value={assessNotes}
                  onChange={e => setAssessNotes(e.target.value)}
                />
              </div>
            </details>
            <div className="btn-row" style={{ marginTop: 14 }}>
              <button className="btn" onClick={submitAssessment}>
                Record assessment
              </button>
            </div>
          </section>

          <section className="card">
            <h2 className="card-title step-title">
              <span className="step">2</span>Transplant record
            </h2>
            <p className="card-hint">
              An assessment does not imply a transplant.
            </p>
            <div className="grid-2">
              <div className="field">
                <label className="label" htmlFor="tx-plant">
                  Plant
                </label>
                <select
                  id="tx-plant"
                  className="select"
                  value={txPlant}
                  onChange={e => setTxPlant(e.target.value)}
                >
                  <option value="">Select a plant…</option>
                  {plants.map(p => (
                    <option key={p.plant_id} value={p.plant_id}>
                      {plantLabel(p)}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label className="label" htmlFor="tx-date">
                  Transplant date
                </label>
                <input
                  id="tx-date"
                  className="input"
                  type="date"
                  value={txDate}
                  onChange={e => setTxDate(e.target.value)}
                />
              </div>
            </div>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={txOccurred}
                onChange={e => setTxOccurred(e.target.checked)}
              />
              The transplant actually occurred
            </label>
            <details className="disclosure">
              <summary>More options</summary>
              <div className="grid-2" style={{ marginTop: 10 }}>
                <div className="field">
                  <label className="label" htmlFor="tx-height">
                    Height at transplant (cm)
                  </label>
                  <input
                    id="tx-height"
                    className="input"
                    type="number"
                    step="0.1"
                    value={txHeight}
                    onChange={e => setTxHeight(e.target.value)}
                  />
                </div>
                <div className="field">
                  <label className="label" htmlFor="tx-condition">
                    Condition at transplant
                  </label>
                  <select
                    id="tx-condition"
                    className="select"
                    value={txCondition}
                    onChange={e => setTxCondition(e.target.value)}
                  >
                    <option value="">(not recorded)</option>
                    <option value="healthy">healthy</option>
                    <option value="stressed_wilting">stressed_wilting</option>
                    <option value="dead">dead</option>
                  </select>
                </div>
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label className="label" htmlFor="tx-notes">
                  Notes
                </label>
                <textarea
                  id="tx-notes"
                  className="textarea"
                  value={txNotes}
                  onChange={e => setTxNotes(e.target.value)}
                />
              </div>
            </details>
            <div className="btn-row" style={{ marginTop: 14 }}>
              <button className="btn" onClick={submitTransplant}>
                Record transplant
              </button>
            </div>
          </section>

          <section className="card">
            <h2 className="card-title step-title">
              <span className="step">3</span>14-day follow-up
            </h2>
            <p className="card-hint">
              Missing feedback is never treated as death or failure.
            </p>

            {pending.length === 0 ? (
              <div className="alert alert-warning" style={{ marginBottom: 14 }}>
                No follow-ups are currently due.
              </div>
            ) : (
              <div className="table-wrap" style={{ marginBottom: 16 }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th>#</th>
                      <th>Plant</th>
                      <th>Transplant</th>
                      <th>Due</th>
                      <th>State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pending.map(f => (
                      <tr
                        key={f.feedback_id}
                        className={
                          'is-clickable' +
                          (String(f.feedback_id) === followFeedback
                            ? ' is-selected'
                            : '')
                        }
                        onClick={() => setFollowFeedback(String(f.feedback_id))}
                      >
                        <td>{f.feedback_id}</td>
                        <td>
                          {f.plant_id} {f.species}
                        </td>
                        <td>{f.transplant_date}</td>
                        <td>{f.feedback_due_date}</td>
                        <td>
                          {f.feedback_state?.status}
                          {f.feedback_state?.days_overdue
                            ? ` (${f.feedback_state.days_overdue}d)`
                            : ''}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            <div className="grid-2">
              <div className="field">
                <label className="label" htmlFor="follow-feedback">
                  Feedback record
                </label>
                <input
                  id="follow-feedback"
                  className="input"
                  value={followFeedback}
                  onChange={e => setFollowFeedback(e.target.value)}
                  placeholder="select a row above"
                />
              </div>
              <div className="field">
                <label className="label" htmlFor="follow-date">
                  Submission date
                </label>
                <input
                  id="follow-date"
                  className="input"
                  type="date"
                  value={followDate}
                  onChange={e => setFollowDate(e.target.value)}
                />
              </div>
              <div className="field">
                <label className="label" htmlFor="follow-survived">
                  Survived after 14d
                </label>
                <select
                  id="follow-survived"
                  className="select"
                  value={followSurvived ? 'yes' : 'no'}
                  onChange={e => setFollowSurvived(e.target.value === 'yes')}
                >
                  <option value="yes">yes</option>
                  <option value="no">no</option>
                </select>
              </div>
            </div>
            <details className="disclosure">
              <summary>More options</summary>
              <div className="grid-2" style={{ marginTop: 10 }}>
                <div className="field">
                  <label className="label" htmlFor="follow-height">
                    Height after 14d (cm)
                  </label>
                  <input
                    id="follow-height"
                    className="input"
                    type="number"
                    step="0.1"
                    value={followHeight}
                    onChange={e => setFollowHeight(e.target.value)}
                  />
                </div>
                <div className="field">
                  <label className="label" htmlFor="follow-condition">
                    Visible condition
                  </label>
                  <select
                    id="follow-condition"
                    className="select"
                    value={followCondition}
                    onChange={e => setFollowCondition(e.target.value)}
                  >
                    <option value="healthy">healthy</option>
                    <option value="stressed_wilting">stressed_wilting</option>
                    <option value="dead">dead</option>
                  </select>
                </div>
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label className="label" htmlFor="follow-notes">
                  Notes
                </label>
                <textarea
                  id="follow-notes"
                  className="textarea"
                  value={followNotes}
                  onChange={e => setFollowNotes(e.target.value)}
                />
              </div>
            </details>
            <div className="btn-row" style={{ marginTop: 14 }}>
              <button className="btn" onClick={submitFollowUp}>
                Submit follow-up
              </button>
            </div>
          </section>
        </>
      )}

      {tab === 'reference' && (
        <>
          <section className="card">
          <div className="row-between" style={{ marginBottom: 4 }}>
            <h2 className="card-title">Successful-cohort reference</h2>
            {baseline && (
              <button className="btn btn-secondary btn-sm" onClick={buildBaseline}>
                Rebuild
              </button>
            )}
          </div>
          <p className="card-hint">
            Medians from stored real measurements
            {selectedSpecies ? ` for ${selectedSpecies}` : ''}.
          </p>

          {!selectedSpecies ? (
            <p className="muted">Select a species to view its reference.</p>
          ) : baselineMissing ? (
            <div className="alert alert-warning">
              <p className="alert-title">No reference yet</p>
              <p>No reference baseline has been built for {selectedSpecies}.</p>
              <div className="btn-row" style={{ marginTop: 10 }}>
                <button className="btn" onClick={buildBaseline}>
                  Build reference
                </button>
              </div>
            </div>
          ) : baseline ? (
            <>
              <div className="grid">
                <Stat
                  label="Plants contributing"
                  value={fmt(baseline.n_plants_contributing_to_medians)}
                />
                <Stat label="Plants in cohort" value={fmt(baseline.n_plants)} />
                <Stat label="Observations" value={fmt(baseline.n_points)} />
                <Stat
                  label="Transplant reference"
                  value={
                    baseline.transplant_reference?.assumption
                      ? 'Assumed (next day)'
                      : 'Recorded'
                  }
                />
              </div>
              <details className="disclosure">
                <summary>Timeline and limitations</summary>
                {baseline.transplant_reference?.rule && (
                  <p className="muted small" style={{ marginTop: 10 }}>
                    {baseline.transplant_reference.rule}
                  </p>
                )}
                <p className="muted small" style={{ margin: '8px 0' }}>
                  {baseline.by_age_days.length} age groups · median days to
                  transplant ranges{' '}
                  {fmt(
                    Math.min(
                      ...baseline.by_age_days.map(g =>
                        g.median_days_until_transplant ?? 0
                      )
                    )
                  )}{' '}
                  –{' '}
                  {fmt(
                    Math.max(
                      ...baseline.by_age_days.map(g =>
                        g.median_days_until_transplant ?? 0
                      )
                    )
                  )}{' '}
                  days
                </p>
                <div className="subhead">Limitations</div>
                <ul className="list" style={{ marginTop: 0 }}>
                  {baseline.limitations.map((l, i) => (
                    <li key={i}>{l}</li>
                  ))}
                </ul>
              </details>
            </>
          ) : (
            <p className="muted">Loading reference…</p>
          )}
        </section>

        <section className="card" style={{ marginTop: 16 }}>
          <h2 className="card-title">Add dataset (CSV)</h2>
          <p className="card-hint">
            Upload a measurement CSV (Date, Age/Age_days, Plant_ID, Height_cm;
            growth rate and widths are optional) to bootstrap a reference
            baseline and start predicting from day 1. The whole file must
            validate before anything is written.
          </p>
          <div className="field">
            <label className="label" htmlFor="upload-species">Species</label>
            <input
              id="upload-species"
              className="input"
              value={uploadSpecies}
              onChange={e => setUploadSpecies(e.target.value)}
              placeholder={selectedSpecies || 'e.g. Sunflower (SuperOrange)'}
            />
            <p className="muted small" style={{ marginTop: 6 }}>
              Required. Defaults to the selected species above if left blank.
            </p>
          </div>
          <div className="field">
            <label className="label" htmlFor="upload-file">CSV file</label>
            <input
              id="upload-file"
              className="input"
              type="file"
              accept=".csv,text/csv,text/plain"
              onChange={e => {
                const f = e.target.files && e.target.files[0]
                if (!f) return
                const reader = new FileReader()
                reader.onload = () => setUploadCsv(String(reader.result || ''))
                reader.readAsText(f)
              }}
            />
            <p className="muted small" style={{ marginTop: 6 }}>
              Or paste the CSV below.
            </p>
          </div>
          <div className="field">
            <label className="label" htmlFor="upload-csv">CSV contents</label>
            <textarea
              id="upload-csv"
              className="textarea"
              rows={8}
              value={uploadCsv}
              onChange={e => setUploadCsv(e.target.value)}
              placeholder={'Date,Age_days,Plant_ID,Height_cm\n2083-05-02,2,P1,1.0'}
            />
          </div>
          <div className="btn-row" style={{ marginTop: 14 }}>
            <button
              className="btn"
              onClick={submitUpload}
              disabled={uploadBusy}
            >
              {uploadBusy ? 'Uploading…' : 'Upload dataset'}
            </button>
          </div>
          {uploadResult && (
            <div className="alert alert-success" style={{ marginTop: 12 }}>
              {uploadResult}
            </div>
          )}
        </section>
      </>
      )}

      {tab === 'evaluation' && (
        <section className="card">
          <h2 className="card-title">Evaluation</h2>
          <p className="card-hint">
            Predicted vs observed transplant dates. Models are never promoted
            automatically.
          </p>
          {!evaluation ? (
            <p className="muted">Loading…</p>
          ) : (
            <>
              <div className="grid">
                <Stat
                  label="Predictions evaluated"
                  value={fmt(evaluation.n_predictions_evaluated)}
                />
                <Stat
                  label="Independent plants"
                  value={fmt(evaluation.n_independent_plants)}
                />
                <Stat
                  label="Mean abs error"
                  value={fmt(evaluation.overall.mean_abs_error_days, ' d')}
                />
              </div>

              <div
                className={
                  'alert ' +
                  (evaluation.sufficient_evidence
                    ? 'alert-success'
                    : 'alert-warning')
                }
                style={{ marginTop: 16 }}
              >
                {evaluation.sufficient_evidence
                  ? 'Enough independent evidence has accumulated to compare methods.'
                  : 'Not enough independent evidence yet — predictions are not being promoted or retrained.'}
              </div>

              <div className="stat" style={{ marginTop: 4 }}>
                <div className="stat-label">Comparison (never auto-applied)</div>
                <p style={{ marginTop: 8, marginBottom: 6 }}>
                  {evaluation.comparison.recommendation}
                </p>
                <p className="muted small" style={{ margin: 0 }}>
                  Methods compared:{' '}
                  {evaluation.comparison.methods_compared.join(', ') || '—'} ·
                  auto-applied: {String(evaluation.comparison.auto_applied)}
                </p>
              </div>

              <details className="disclosure">
                <summary>More details</summary>
                <div className="grid" style={{ marginTop: 12 }}>
                  <Stat
                    label="Matched observed date"
                    value={
                      evaluation.overall.match_rate === null
                        ? '—'
                        : `${Math.round(evaluation.overall.match_rate * 100)}%`
                    }
                  />
                  <Stat
                    label="14-day outcomes"
                    value={fmt(evaluation.overall.n_with_14d_outcome)}
                  />
                  <Stat
                    label="Incomplete feedback"
                    value={fmt(evaluation.overall.n_incomplete_feedback)}
                  />
                </div>
                {evaluation.notes.length > 0 && (
                  <ul className="list">
                    {evaluation.notes.map((n, i) => (
                      <li key={i}>{n}</li>
                    ))}
                  </ul>
                )}
              </details>

              <div className="subhead">Prediction history</div>
              {predictions.length === 0 ? (
                <p className="muted small" style={{ margin: 0 }}>
                  No predictions recorded yet.
                </p>
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>#</th>
                        <th>Plant</th>
                        <th>Method</th>
                        <th className="num">Countdown</th>
                        <th>Readiness date</th>
                        <th>Created</th>
                      </tr>
                    </thead>
                    <tbody>
                      {predictions.map(p => (
                        <tr key={p.prediction_id}>
                          <td>{p.prediction_id}</td>
                          <td>{p.plant_id ?? '—'}</td>
                          <td>
                            {p.method}
                            {p.provisional ? (
                              <span
                                className="badge badge-info"
                                style={{ marginLeft: 8 }}
                              >
                                provisional
                              </span>
                            ) : null}
                          </td>
                          <td className="num">
                            {countdownText(p.days_remaining)}
                          </td>
                          <td>{fmt(p.predicted_readiness_date)}</td>
                          <td>{p.created_at?.split('T')[0]}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </section>
      )}
    </Layout>
  )
}
