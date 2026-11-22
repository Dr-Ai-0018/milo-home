import type { components } from './schema'
import { ApiFailure } from './client'

const DOWNLOAD_TIMEOUT_MS = 120_000

/** Parse a filename from Content-Disposition (RFC 5987 filename* or quoted filename). */
export function parseDispositionFilename(header: string | null): string | null {
  if (!header) return null
  const star = header.match(/filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;]+)/i)
  if (star?.[1]) {
    try {
      const decoded = decodeURIComponent(star[1].trim().replace(/^"(.*)"$/, '$1'))
      if (decoded) return decoded
    } catch {
      // Fall through to plain filename.
    }
  }
  const plain = header.match(/filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;\s]+)/i)
  const raw = (plain?.[1] ?? plain?.[2] ?? '').trim()
  return raw ? raw.split(/[\\/]/).pop() || null : null
}

/** Fetch a same-origin binary response, parse JSON error envelopes on failure, and trigger browser download. */
export async function downloadApiFile(
  path: string,
  signal: AbortSignal,
  fallbackName: string,
  mediaType: 'application/octet-stream' | 'application/zip',
  body?: unknown,
): Promise<string> {
  const headers = new Headers({ Accept: mediaType })
  if (body !== undefined) headers.set('Content-Type', 'application/json')
  const own = AbortSignal.any([signal, AbortSignal.timeout(DOWNLOAD_TIMEOUT_MS)])
  let response: Response
  try {
    response = await fetch(`${import.meta.env.BASE_URL}api${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'error',
      signal: own,
    })
  } catch {
    throw new ApiFailure(0, 'network')
  }
  if (!response.ok) {
    let code = 'unknown'
    try {
      if (response.headers.get('content-type')?.includes('application/json')) {
        const data = (await response.json()) as Partial<
          components['schemas']['ErrorEnvelope']
        > | null
        if (typeof data?.error?.code === 'string') code = data.error.code
      }
    } catch {
      // Keep fallback code when error payload cannot be parsed.
    }
    throw new ApiFailure(response.status, code)
  }
  const receivedType = response.headers.get('content-type')?.split(';')[0]?.trim().toLowerCase()
  if (response.status !== 200 || receivedType !== mediaType) {
    void response.body?.cancel().catch(() => undefined)
    throw new ApiFailure(502, 'invalid_download')
  }
  let blob: Blob
  try {
    blob = await response.blob()
    own.throwIfAborted()
  } catch {
    throw new ApiFailure(0, 'network')
  }
  const filename =
    parseDispositionFilename(response.headers.get('content-disposition')) ?? fallbackName
  const url = URL.createObjectURL(blob)
  try {
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    link.rel = 'noopener'
    link.style.display = 'none'
    document.body.append(link)
    link.click()
    link.remove()
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 1_000)
  }
  return filename
}
