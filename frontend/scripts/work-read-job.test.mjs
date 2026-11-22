import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createWorkReadJob } from '../src/utils/workReadJob.ts'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

test('polling and repeated retry share one in-flight read', async () => {
  const gate = deferred()
  let calls = 0
  const job = createWorkReadJob(async () => {
    calls += 1
    await gate.promise
  })
  const first = job.run()
  assert.equal(job.run(), first)
  await Promise.resolve()
  assert.equal(calls, 1)
  gate.resolve()
  await first
  await job.run()
  assert.equal(calls, 2)
})

test('writes during an old read queue one fresh read, even after an old failure', async () => {
  const gate = deferred()
  let calls = 0
  const job = createWorkReadJob(async () => {
    if (++calls === 1) await gate.promise
  })
  const first = job.run()
  await Promise.resolve()
  assert.equal(job.run(true), first)
  job.run(true)
  gate.reject(new Error('old snapshot failed'))
  await first
  assert.equal(calls, 2)
})

test('reset prevents old completion from clearing the new task slot or queuing stale reads', async () => {
  const firstGate = deferred()
  const secondGate = deferred()
  let calls = 0
  const job = createWorkReadJob(async () => {
    calls += 1
    await (calls === 1 ? firstGate.promise : secondGate.promise)
  })
  const old = job.run()
  await Promise.resolve()
  job.run(true)
  job.reset()
  const current = job.run()
  await Promise.resolve()
  firstGate.resolve()
  await old
  assert.equal(job.run(), current)
  secondGate.resolve()
  await current
  assert.equal(calls, 2)
})

test('a failed read releases its slot for a manual retry', async () => {
  let calls = 0
  const job = createWorkReadJob(async () => {
    if (++calls === 1) throw new Error('temporary failure')
  })
  await assert.rejects(job.run(), /temporary failure/)
  await job.run()
  assert.equal(calls, 2)
})

test('reset before dispatch does not read the next task from an old scheduled job', async () => {
  let calls = 0
  const job = createWorkReadJob(async () => {
    calls += 1
  })
  const abandoned = job.run()
  job.reset()
  await abandoned
  assert.equal(calls, 0)
})
