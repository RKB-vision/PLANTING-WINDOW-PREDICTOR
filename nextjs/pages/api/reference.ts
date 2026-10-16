import type { NextApiRequest, NextApiResponse } from 'next'
import axios from 'axios'

const PYTHON_BACKEND_URL = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000'

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  try {
    if (req.method === 'GET') {
      const resp = await axios.get(`${PYTHON_BACKEND_URL}/api/reference`, {
        params: req.query,
      })
      return res.status(200).json(resp.data)
    }
    if (req.method === 'POST') {
      const resp = await axios.post(
        `${PYTHON_BACKEND_URL}/api/reference/build`,
        req.body || {}
      )
      return res.status(resp.status).json(resp.data)
    }
  } catch (e: any) {
    const status = e.response?.status || 500
    const data = e.response?.data || { error: 'Reference baseline request failed' }
    return res.status(status).json(data)
  }
  res.setHeader('Allow', ['GET', 'POST'])
  return res.status(405).json({ error: 'Method Not Allowed' })
}
