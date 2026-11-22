const positions = new Map<string, number>()

/** Save current vertical scroll offset before leaving a route. */
export function rememberScrollPosition(path: string, y: number) {
  positions.set(path, y)
}

/** Return the previously stored vertical scroll offset for a list route. */
export function savedScrollPosition(path: string) {
  return positions.get(path)
}

/** Clear stored scroll position (for instance after permission revocation or logout). */
export function forgetScrollPosition(path: string) {
  positions.delete(path)
}
