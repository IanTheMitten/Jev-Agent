// The import's pure decisions, kept apart from the Electron/SDK wiring in
// web-import.ts so they can be exercised without a live guest.

export type PenImportMode = 'page' | 'selection'

export interface PenImportOptions {
  /** CSS selector of the one element to import; a live pick or the whole page when absent. */
  selector?: string
  mode?: PenImportMode
}

/** A selector wins; otherwise `mode`, defaulting to the whole page. */
export function resolveImportMode(options: PenImportOptions): PenImportMode {
  return options.selector || options.mode === 'selection' ? 'selection' : 'page'
}

/**
 * Selector for the ancestor `steps` levels above `selector`, via `:has(> …)`
 * — the pick's path only carries display labels, never selectors, so the
 * crumbs hover by climbing from the picked element. 0 steps is the element
 * itself; a negative step (a crumb below the pick) has no target.
 */
export function ancestorSelector(selector: string, steps: number): string | undefined {
  if (steps < 0) {
    return undefined
  }

  if (steps === 0) {
    return selector
  }

  return `*:has(> ${'* > '.repeat(steps - 1)}${selector})`
}
