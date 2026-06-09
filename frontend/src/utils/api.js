import axios from 'axios'

const api = axios.create({
  baseURL: 'https://tradesim-backend-1b74.onrender.com',
  withCredentials: true
})

// ── Auth ───────────────────────────────────────────────────────────────
export const register   = (payload)                        => api.post('/auth/register', payload)
export const login      = (payload)                        => api.post('/auth/login',    payload)
export const logout     = ()                               => api.post('/auth/logout')
export const getMe      = ()                               => api.get('/auth/me')

// ── Market data ────────────────────────────────────────────────────────
export const getChart   = (exchange, symbol, period = '100d', interval = '1d') =>
  api.get(`/chart/${exchange}/${symbol}?period=${period}&interval=${interval}`)
export const getPrice   = (exchange, symbol)               => api.get(`/price/${exchange}/${symbol}`)
export const getSymbols = (exchange)                       => api.get(`/symbols/${exchange}`)

// ── Trading ────────────────────────────────────────────────────────────
export const getWallet  = ()                               => api.get('/wallet')
export const getTrades  = ()                               => api.get('/trades')
export const placeOrder = (payload)                        => api.post('/order', payload)
export const resetSim   = ()                               => api.post('/reset')
