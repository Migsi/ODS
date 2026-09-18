import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import ExternalLemonadeAdoption from './ExternalLemonadeAdoption'

const loaded = { status: 'verified', modelId: 'Qwen3.5-2B-Q4_K_M', contextLength: 65536, backend: 'vulkan' }
const response = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body })
const view = (props = {}) => render(
  <MemoryRouter><ExternalLemonadeAdoption enabled minimumContext={16384} {...props} /></MemoryRouter>
)

afterEach(() => vi.unstubAllGlobals())

test('keeps nonexternal Lemonade installations free of an adoption control', async () => {
  const fetch = vi.fn().mockResolvedValue(response({}, 409))
  vi.stubGlobal('fetch', fetch)
  view()
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
  expect(screen.queryByRole('region', { name: 'External Lemonade model' })).toBeNull()
})

test('rechecks the exact physical model before one adoption and refreshes ODS', async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(response(loaded))
    .mockResolvedValueOnce(response(loaded))
    .mockResolvedValueOnce(response({ status: 'adopted', modelId: loaded.modelId }))
  vi.stubGlobal('fetch', fetch)
  const onSettled = vi.fn()
  view({ onSettled })
  fireEvent.click(await screen.findByRole('button', { name: 'Adopt loaded model in ODS' }))
  await waitFor(() => expect(onSettled).toHaveBeenCalledTimes(1))
  expect(fetch).toHaveBeenCalledTimes(3)
  expect(fetch.mock.calls[2][0]).toBe('/api/models/external-adopt')
  expect(JSON.parse(fetch.mock.calls[2][1].body)).toEqual({ model_id: loaded.modelId })
})

test('a changed external model must be reviewed before adoption', async () => {
  const changed = { ...loaded, modelId: 'different-model' }
  const fetch = vi.fn().mockResolvedValueOnce(response(loaded)).mockResolvedValueOnce(response(changed))
  vi.stubGlobal('fetch', fetch)
  view()
  fireEvent.click(await screen.findByRole('button', { name: 'Adopt loaded model in ODS' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('changed')
  expect(screen.getByText('different-model')).toBeVisible()
  expect(fetch).toHaveBeenCalledTimes(2)
})

test('a refresh error does not misreport a committed adoption as failed', async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(response(loaded))
    .mockResolvedValueOnce(response(loaded))
    .mockResolvedValueOnce(response({ status: 'adopted', modelId: loaded.modelId }))
  vi.stubGlobal('fetch', fetch)
  view({ onSettled: vi.fn().mockRejectedValue(new Error('refresh unavailable')) })
  fireEvent.click(await screen.findByRole('button', { name: 'Adopt loaded model in ODS' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('model was adopted')
  expect(fetch).toHaveBeenCalledTimes(3)
})

test('pending adoption tells the user Pixel remains held', async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(response(loaded))
    .mockResolvedValueOnce(response(loaded))
    .mockResolvedValueOnce(response({ detail: {
      error: 'Adoption is incomplete', code: 'managed_model_recovery_required', pending: true,
    } }, 503))
  vi.stubGlobal('fetch', fetch)
  view()
  fireEvent.click(await screen.findByRole('button', { name: 'Adopt loaded model in ODS' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Adoption is incomplete')
  expect(screen.getByText(/Pixel stays held until recovery/)).toBeVisible()
  expect(screen.getByRole('link', { name: 'Open Pixel recovery' })).toHaveAttribute('href', '/pixel')
})

test('insufficient context cannot be adopted for managed Pixel', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ ...loaded, contextLength: 8192 })))
  view()
  const button = await screen.findByRole('button', { name: 'Adopt loaded model in ODS' })
  expect(button).toBeDisabled()
  expect(screen.getByText(/Pixel needs at least/)).toBeVisible()
})
