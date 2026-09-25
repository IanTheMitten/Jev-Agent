import assert from 'node:assert/strict'

import { test } from 'vitest'

import { ancestorSelector, resolveImportMode } from './web-import-select'

test('a selector always means a selection import, whatever mode says', () => {
  assert.equal(resolveImportMode({ selector: '#hero', mode: 'page' }), 'selection')
  assert.equal(resolveImportMode({ mode: 'selection' }), 'selection')
  assert.equal(resolveImportMode({}), 'page')
})

test('ancestor selectors climb exactly `steps` levels from the picked element', () => {
  const pick = 'h1.lede'

  assert.equal(ancestorSelector(pick, 0), pick)
  assert.equal(ancestorSelector(pick, 1), '*:has(> h1.lede)')
  assert.equal(ancestorSelector(pick, 3), '*:has(> * > * > h1.lede)')
  assert.equal(ancestorSelector(pick, -1), undefined)
})
