export interface HomeRecentNote {
  id: string
  date: string
  isoDate: string
  category: string
  title: string
  text: string
  lead?: boolean
}

export const SCHEDULE_BOUNDARY_COPY =
  '这里显示北京时间的作息安排；它不表示米洛此刻在线、已经接单，或会自动回信。'

// 经核对后的公开近况：最新在前，仅展示已经完成且可公开的小家变化。
export const HOME_RECENT_NOTES: readonly HomeRecentNote[] = [
  {
    id: 'work-status-filter',
    date: '11 月 14 日',
    isoDate: '2026-11-14',
    category: '交办工作桌',
    title: '交办可以按状态找了',
    text: '想看还在做的、等核对的，或已经结束的，都可以从交办列表直接切过去。',
    lead: true,
  },
  {
    id: 'work-receipt',
    date: '11 月 14 日',
    isoDate: '2026-11-14',
    category: '回执与交付',
    title: '在原单上就能看回执',
    text: '打开交办单就能看这次处理的答复；如果还没有回执，页面会说明。',
  },
  {
    id: 'work-attachments',
    date: '11 月 14 日',
    isoDate: '2026-11-14',
    category: '回执与交付',
    title: '交办交付的文件可以下载了',
    text: '有文件交付时，从原单下载，挑几份或一起打包都行。',
  },
]
