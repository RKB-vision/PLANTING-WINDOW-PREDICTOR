import type { NextApiRequest, NextApiResponse } from 'next'
import axios from 'axios'

const PYTHON_BACKEND_URL = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000'

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', ['POST'])
    return res.status(405).json({ error: 'Method Not Allowed' })
  }

  try {
    const resp = await axios.post(
      `${PYTHON_BACKEND_URL}/api/import`,
      req.body || {},
      {
        headers: { 'Content-Type': req.headers['content-type'] || 'application/json' },
      }
    )
    return res.status(resp.status).json(resp.data)
  } catch (e: any) {
    const status = e.response?.status || 500
    const data = e.response?.data || { error: 'Dataset import failed' }
    return res.status(status).json(data)
  }
}
