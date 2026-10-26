import { useEffect, useState } from 'react'
import axios from 'axios'
import Layout from '../components/Layout'

interface Capture {
  capture_id: number
  public_id: string
  image_url: string | null
  capture_timestamp: string | null
  observation_date: string
  height_cm: number | null
  stem_width_cm: number | null
  visible_true_leaf_count: number | null
  confidence: number | null
  validity: string
  problem: string | null
  calibration_verified: number
  calibration_source: string
  review_status: string
}

interface Group {
  plant_id: number
  species: string
  variety: string | null
  external_ref: string | null
  observation_date: string
  captures: Capture[]
  proposed: {
    median_height_cm: number | null
    median_stem_width_cm: number | null
    median_visible_true_leaf_count: number | null
    n_valid: number
    n_total: number
  }
}

export default function ReadingsPage() {
  const [groups, setGroups] = useState<Group[]>([])
  const [pendingCount, setPendingCount] = useState(0)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [status, setStatus] = useState<'pending' | 'all'>('pending')
  const [speciesFilter, setSpeciesFilter] = useState('')
  const [speciesList, setSpeciesList] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    setError(null)
    try {
      const resp = await axios.get('/api/captures', {
        params: { status, species: speciesFilter || undefined },
      })
      const data = resp.data
      setGroups(data.groups || [])
      setPendingCount(data.pending_count || 0)
      const sp = Array.from(
        new Set((data.groups || []).map((g: Group) => g.species))
      ) as string[]
      if (!speciesList.length) setSpeciesList(sp)
      setSelected(new Set())
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Failed to load readings.')
    }
  }

  useEffect(() => {
    load()
  }, [status, speciesFilter])

  const toggle = (id: number) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const review = async (action: 'accept' | 'discard') => {
    if (!selected.size) return
    setBusy(true)
    setError(null)
    setMessage(null)
    try {
      const resp = await axios.post('/api/captures/review', {
        ids: Array.from(selected),
        action,
      })
      setMessage(
        `${resp.data.reviewed} reading(s) ${action === 'accept' ? 'accepted' : 'discarded'}.`
      )
      await load()
    } catch (e: any) {
      setError(e?.response?.data?.error || 'Review failed.')
    } finally {
      setBusy(false)
    }
  }

  const acceptAll = async (g: Group) => {
    const ids = g.captures
      .filter(c => c.review_status === 'pending')
      .map(c => c.capture_id)
    if (!ids.length) return
    setSelected(new Set(ids))
    await review('accept')
  }

  return (
    <Layout active={'readings' as any}>
      <div className="page-head">
        <h1 className="card-title" style={{ margin: 0 }}>Readings</h1>
        <p className="page-sub">
          Review OpenCV measurements before they are used for prediction.
        </p>
      </div>

      {error && (
        <div className="alert alert-danger">
          <p>{error}</p>
        </div>
      )}
      {message && (
        <div className="alert alert-success">
          <p>{message}</p>
        </div>
      )}

      <section className="card" style={{ marginBottom: 16 }}>
        <div className="grid-2">
          <div className="field">
            <label className="label" htmlFor="readings-status">Status</label>
            <select
              id="readings-status"
              className="select"
              value={status}
              onChange={e => setStatus(e.target.value as 'pending' | 'all')}
            >
              <option value="pending">Pending review</option>
              <option value="all">All</option>
            </select>
          </div>
          <div className="field">
            <label className="label" htmlFor="readings-species">Species</label>
            <select
              id="readings-species"
              className="select"
              value={speciesFilter}
              onChange={e => setSpeciesFilter(e.target.value)}
            >
              <option value="">All species</option>
              {speciesList.map(sp => (
                <option key={sp} value={sp}>{sp}</option>
              ))}
            </select>
          </div>
        </div>
        <p className="muted small" style={{ margin: '8px 0 0' }}>
          {pendingCount} reading(s) pending review.
        </p>
      </section>

      {groups.length === 0 && (
        <div className="card">
          <p className="muted">No readings match the current filters.</p>
        </div>
      )}

      {groups.map(g => (
        <section className="card" key={`${g.plant_id}-${g.observation_date}`} style={{ marginBottom: 16 }}>
          <div className="row-between" style={{ marginBottom: 8 }}>
            <div>
              <h2 className="card-title" style={{ margin: 0 }}>
                {g.external_ref || `Plant ${g.plant_id}`} · {g.species}
                {g.variety ? ` / ${g.variety}` : ''}
              </h2>
              <p className="muted small" style={{ margin: 0 }}>
                {g.observation_date} · proposed median:{' '}
                {g.proposed.median_height_cm != null
                  ? `${g.proposed.median_height_cm} cm height`
                  : 'no usable height'}{' '}
                · {g.proposed.n_valid}/{g.proposed.n_total} valid
              </p>
            </div>
            <button
              className="btn btn-secondary btn-sm"
              onClick={() => acceptAll(g)}
              disabled={busy}
            >
              Accept all valid
            </button>
          </div>

          <div className="grid">
            {g.captures.map(c => (
              <div
                key={c.capture_id}
                className="card"
                style={{ padding: 10, opacity: c.review_status === 'discarded' ? 0.5 : 1 }}
              >
                {c.image_url && (
                  <img
                    src={c.image_url}
                    alt={c.public_id}
                    style={{ width: '100%', height: 160, objectFit: 'cover', borderRadius: 6 }}
                  />
                )}
                <p className="small" style={{ margin: '6px 0 0', fontWeight: 600 }}>
                  {c.public_id}
                </p>
                <p className="muted small" style={{ margin: 0 }}>
                  {c.capture_timestamp || '—'} ·{' '}
                  {c.calibration_verified ? 'calibrated' : 'unverified calibration'}
                </p>
                <p className="small" style={{ margin: '4px 0 0' }}>
                  Height {c.height_cm != null ? `${c.height_cm} cm` : '—'} ·{' '}
                  Stem {c.stem_width_cm != null ? `${c.stem_width_cm} cm` : '—'} ·{' '}
                  Leaves {c.visible_true_leaf_count != null ? c.visible_true_leaf_count : '—'}
                </p>
                {c.confidence != null && (
                  <p className="muted small" style={{ margin: 0 }}>
                    Confidence {c.confidence}
                  </p>
                )}
                {c.validity !== 'valid' && (
                  <p className="small" style={{ margin: '4px 0 0', color: 'var(--danger, #b00020)' }}>
                    {c.problem || c.validity}
                  </p>
                )}
                <p className="muted small" style={{ margin: '4px 0 0' }}>
                  Status: {c.review_status}
                </p>
                {c.review_status === 'pending' && (
                  <label className="checkbox-row" style={{ marginTop: 6 }}>
                    <input
                      type="checkbox"
                      checked={selected.has(c.capture_id)}
                      onChange={() => toggle(c.capture_id)}
                    />
                    Select
                  </label>
                )}
              </div>
            ))}
          </div>
        </section>
      ))}

      {selected.size > 0 && (
        <div className="btn-row" style={{ position: 'sticky', bottom: 12 }}>
          <button
            className="btn"
            disabled={busy}
            onClick={() => review('accept')}
          >
            Accept selected ({selected.size})
          </button>
          <button
            className="btn btn-secondary"
            disabled={busy}
            onClick={() => review('discard')}
          >
            Discard selected
          </button>
        </div>
      )}
    </Layout>
  )
}