import type { Schedule } from '../api/client'
import { handCopy } from '../theme/handCopy'
import { clockTime, dayLabel, sameDay } from './time'

export interface PhaseView {
  stamp: string
  line: string
  aside: string
  resting: boolean
}

// 自动叫醒未启用，文案只描述作息与收信，不承诺什么时候一定会读。
// 手写旁白只补充时间点；没有时间可补时留空，免得和印章、正文重复同一句话。
export function describePhase(schedule: Schedule | null, problem: string): PhaseView {
  if (!schedule) {
    return {
      stamp: problem ? '看不清' : '看一眼…',
      line: problem ? '暂时读不到米洛的作息。信照常可以留。' : '正在看米洛现在在做什么。',
      aside: '',
      resting: false,
    }
  }

  const next = schedule.next_change_at
  const nextTime = next ? clockTime(next) : null

  switch (schedule.phase) {
    case 'working': {
      const toWindingDown =
        nextTime !== null &&
        schedule.today?.windows.some(
          (window) => window.start === nextTime && window.phase === 'winding_down',
        )
      return {
        stamp: '工作时段',
        line: '现在是米洛的工作时段。信先收好，他来读信时就能看到。',
        aside:
          toWindingDown && nextTime
            ? handCopy.phaseAside.toWindingDown(nextTime)
            : handCopy.phaseAside.working(nextTime),
        resting: false,
      }
    }
    case 'winding_down':
      return {
        stamp: '收尾中',
        line: '米洛在收拾桌面，不开新的事。现在留的信，下次读信时看。',
        aside: handCopy.phaseAside.windingDown(nextTime),
        resting: false,
      }
    case 'resting': {
      let when: string | null = null
      if (next && nextTime) {
        if (sameDay(next, schedule.observed_at)) when = nextTime
        else if (sameDay(next, schedule.observed_at + 86400))
          when = handCopy.phaseAside.tomorrow(nextTime)
        else when = `${dayLabel(next, schedule.observed_at)} ${nextTime}`
      }
      return {
        stamp: '睡着了',
        line: '米洛休息了。信照收，不会吵醒他。',
        aside: handCopy.phaseAside.resting(when),
        resting: true,
      }
    }
    case 'paused':
      return {
        stamp: '暂停中',
        line: '米洛的安排暂时停下了，信照常收好。',
        aside: '',
        resting: false,
      }
    default:
      return {
        stamp: '没排班',
        line: '米洛的作息还没定下来，信照常收好。',
        aside: '',
        resting: false,
      }
  }
}
