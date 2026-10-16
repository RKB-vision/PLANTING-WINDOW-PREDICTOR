import type { NextApiRequest, NextApiResponse } from 'next'
import axios from 'axios'

const PYTHON_BACKEND_URL = process.env.PYTHON_BACKEND_URL || 'http://localhost:8000'

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method === 'GET') {
    try {
      const resp = await axios.get(`${PYTHON_BACKEND_URL}/plants`)
      return res.status(200).json(resp.data)
    } catch (e: any) {
      const status = e.response?.status || 500
      const data = e.response?.data || { error: 'Failed to list plants' }
      return res.status(status).json(data)
    }
  }

  if (req.method === 'POST') {
    try {
      const resp = await axios.post(`${PYTHON_BACKEND_URL}/register`, req.body)
      return res.status(resp.status).json(resp.data)
    } catch (e: any) {
      const status = e.response?.status || 500
      const data = e.response?.data || { error: 'Failed to register plant' }
      return res.status(status).json(data)
    }
  }

  res.setHeader('Allow', ['GET', 'POST'])
  return res.status(405).json({ error: 'Method Not Allowed' })
}
