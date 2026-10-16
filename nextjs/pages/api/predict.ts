import type { NextApiRequest, NextApiResponse } from 'next'
import axios from 'axios'

/**
 * POST /api/predict
 * Expected body: { plantId: number }
 * Returns: { available: boolean, message?: string, predictedReadinessDate?: string }
 *
 * This stub forwards the request to the Python backend's prediction endpoint.
 * Adjust `PYTHON_BACKEND_URL` to match your deployment (e.g., http://localhost:8000).
 */
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', ['POST'])
    return res.status(405).json({ error: 'Method Not Allowed' })
  }

  const { plantId } = req.body as { plantId: number }
  if (!plantId) {
    return res.status(400).json({ error: 'Missing plantId' })
  }

  const PYTHON_BACKEND_URL = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000'

  try {
    const response = await axios.post(`${PYTHON_BACKEND_URL}/predict`, { plantId })
    // Forward whatever the Python service returns
    return res.status(200).json(response.data)
  } catch (error: any) {
    console.error('Prediction error:', error)
    const status = error.response?.status || 500
    const data = error.response?.data || { error: 'Prediction service failed' }
    return res.status(status).json(data)
  }
}
