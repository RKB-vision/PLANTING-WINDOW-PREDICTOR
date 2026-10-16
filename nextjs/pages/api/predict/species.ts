import type { NextApiRequest, NextApiResponse } from 'next'
import axios from 'axios'

const PYTHON_BACKEND_URL = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000'

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', ['POST'])
    return res.status(405).json({ error: 'Method Not Allowed' })
  }

  const body = req.body || {}
  const payload = {
    species: body.species,
    variety:
      !body.variety || body.variety === 'All varieties'
        ? null
        : body.variety,
    observation_date: body.observation_date || body.date || null,
    allow_synthetic: Boolean(body.allow_synthetic),
  }

  if (!payload.species) {
    return res.status(400).json({ error: 'species is required' })
  }

  try {
    const resp = await axios.post(
      `${PYTHON_BACKEND_URL}/api/predict/species`,
      payload
    )
    return res.status(200).json(resp.data)
  } catch (e: any) {
    const status = e.response?.status || 500
    const data = e.response?.data || { error: 'Prediction request failed' }
    return res.status(status).json(data)
  }
}
