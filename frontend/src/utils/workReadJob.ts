// 同一路读取合用在途请求；写入后要求一份新快照时，合并成随后的一轮。
export function createWorkReadJob(read: () => Promise<void>) {
  let pending: { promise: Promise<void>; again: boolean } | null = null

  function run(fresh = false): Promise<void> {
    if (pending) {
      pending.again ||= fresh
      return pending.promise
    }
    const job = { promise: Promise.resolve(), again: false }
    pending = job
    job.promise = Promise.resolve().then(async () => {
      if (pending !== job) return
      try {
        do {
          job.again = false
          try {
            await read()
          } catch (error) {
            if (pending !== job || !job.again) throw error
          }
        } while (pending === job && job.again)
      } finally {
        if (pending === job) pending = null
      }
    })
    return job.promise
  }

  // 调用者负责作废数据域与中止请求；旧任务结束不能清除新任务的在途槽。
  function reset() {
    pending = null
  }

  return { run, reset }
}
