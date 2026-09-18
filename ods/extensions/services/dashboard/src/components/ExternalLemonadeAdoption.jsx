import { useCallback, useEffect, useState } from 'react'
import { AlertCircle, RefreshCw } from 'lucide-react'
import { Link } from 'react-router-dom'

function errorText(payload, fallback) {
  const detail = payload?.detail ?? payload
  if (typeof detail?.error === 'string') return detail.error
  if (typeof detail?.message === 'string') return detail.message
  if (typeof detail === 'string') return detail
  return fallback
}

export default function ExternalLemonadeAdoption({ enabled, minimumContext, onSettled }) {
  const [observation, setObservation] = useState(null)
  const [checking, setChecking] = useState(false)
  const [adopting, setAdopting] = useState(false)
  const [error, setError] = useState(null)
  const [pending, setPending] = useState(false)
  const [available, setAvailable] = useState(false)

  const inspect = useCallback(async (signal) => {
    const response = await fetch('/api/models/external-observation', { signal, cache: 'no-store' })
    if (response.status === 409) return null
    const payload = await response.json()
    if (!response.ok || payload?.status !== 'verified' || typeof payload.modelId !== 'string') {
      throw new Error(errorText(payload, 'Could not verify the model loaded in Lemonade.'))
    }
    return payload
  }, [])

  useEffect(() => {
    if (!enabled) return undefined
    const controller = new AbortController()
    setChecking(true)
    void inspect(controller.signal).then(value => {
      setObservation(value)
      setAvailable(Boolean(value))
      setError(null)
    }).catch(cause => {
      if (!controller.signal.aborted) setError(cause.message)
    }).finally(() => {
      if (!controller.signal.aborted) setChecking(false)
    })
    return () => controller.abort()
  }, [enabled, inspect])

  const recheck = async () => {
    setChecking(true)
    setError(null)
    try {
      const value = await inspect()
      setObservation(value)
      setAvailable(Boolean(value))
    } catch (cause) {
      setError(cause.message)
    } finally {
      setChecking(false)
    }
  }

  const adopt = async () => {
    if (!observation || adopting) return
    setAdopting(true)
    setError(null)
    try {
      // The user must see and select the exact physical model. If it changed
      // since the card rendered, require another click after showing the new one.
      const fresh = await inspect()
      if (!fresh || fresh.modelId !== observation.modelId ||
          fresh.contextLength !== observation.contextLength) {
        setObservation(fresh)
        setError('The loaded Lemonade model changed. Review it and retry adoption.')
        return
      }
      const response = await fetch('/api/models/external-adopt', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model_id: fresh.modelId }),
      })
      const payload = await response.json()
      if (!response.ok || payload?.status !== 'adopted' || payload.modelId !== fresh.modelId) {
        setPending(payload?.detail?.pending === true)
        throw new Error(errorText(payload, 'ODS could not confirm model adoption.'))
      }
      setPending(false)
      try {
        await onSettled?.()
      } catch {
        setError('The model was adopted, but the dashboard could not refresh. Reload to check its status.')
      }
    } catch (cause) {
      setError(cause.message)
    } finally {
      setAdopting(false)
    }
  }

  if (!enabled || (!available && !checking && !error)) return null
  const context = Number(observation?.contextLength || 0)
  const tooSmall = context > 0 && context < minimumContext
  return (
    <section aria-label="External Lemonade model" className="mb-5 rounded-xl border border-amber-400/25 bg-amber-500/10 p-4 text-sm text-amber-100">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <AlertCircle size={18} className="mt-0.5 shrink-0" aria-hidden="true" />
          <div>
            <p className="font-semibold">Model managed in Lemonade</p>
            {observation && <p className="mt-1 break-all">Loaded: <strong>{observation.modelId}</strong> · {context.toLocaleString()} context tokens</p>}
            <p className="mt-1 text-amber-100/75">After switching models in Lemonade, adopt the loaded model so Pixel and ODS apps use the same route. ODS will not load or restore the native model.</p>
            {tooSmall && <p className="mt-1">Pixel needs at least {minimumContext.toLocaleString()} context tokens; this model cannot be adopted.</p>}
            {pending && <p className="mt-1">Adoption is pending. Pixel stays held until recovery proves the route. <Link className="underline" to="/pixel">Open Pixel recovery</Link>.</p>}
            {error && <p role="alert" className="mt-2 text-red-300">{error}</p>}
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <button type="button" onClick={recheck} disabled={checking || adopting} className="inline-flex min-h-9 items-center gap-1 rounded-lg border border-amber-300/30 px-3 font-medium disabled:opacity-50">
            <RefreshCw size={14} aria-hidden="true" /> Recheck
          </button>
          <button type="button" onClick={adopt} disabled={!observation || checking || adopting || tooSmall} className="min-h-9 rounded-lg border border-amber-300/50 px-3 font-semibold disabled:opacity-50">
            {adopting ? 'Adopting…' : 'Adopt loaded model in ODS'}
          </button>
        </div>
      </div>
    </section>
  )
}
