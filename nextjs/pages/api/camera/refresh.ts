import type { NextApiRequest, NextApiResponse } from 'next'
import axios from 'axios'

const PYTHON_BACKEND_URL = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000'

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', ['POST'])
    return res.status(405).json({ error: 'Method Not Allowed' })
  }

  try {
    const response = await axios.post(
      `${PYTHON_BACKEND_URL}/api/camera/refresh`,
      req.body || {}
    )
    return res.status(200).json(response.data)
  } catch (error: any) {
    const status = error.response?.status || 500
    const data = error.response?.data || { error: 'Camera refresh failed' }
    return res.status(status).json(data)
  }
}
