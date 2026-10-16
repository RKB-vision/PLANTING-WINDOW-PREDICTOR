import type { NextApiRequest, NextApiResponse } from 'next'
import axios from 'axios'

const PYTHON_BACKEND_URL = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000'

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') {
    res.setHeader('Allow', ['GET'])
    return res.status(405).json({ error: 'Method Not Allowed' })
  }
  try {
    const resp = await axios.get(`${PYTHON_BACKEND_URL}/api/predictions`, {
      params: req.query,
    })
    return res.status(200).json(resp.data)
  } catch (e: any) {
    const status = e.response?.status || 500
    const data = e.response?.data || { error: 'Prediction history request failed' }
    return res.status(status).json(data)
  }
}
