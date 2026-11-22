// 手写体只覆盖本文件和 inks.ts 里的固定文案；改动后按 scripts/subset-hand-font.md 重新生成子集。
// 这里的文字都是装饰或重复说明，关键信息另有标准字体文本承载。

export const handCopy = {
  mastheadAside: '← 套歪了，故意的',
  stampAside: '盖歪了，不许笑',
  issue: (mood: string) => `今天的油墨：${mood}`,
  clockCenter: '今天',
  phaseAside: {
    working: (time: string | null) => (time ? `${time} 歇一会儿` : '在桌前呢'),
    toWindingDown: (time: string) => `${time} 开始收尾`,
    windingDown: (time: string | null) => (time ? `${time} 就收工啦` : '快收工啦'),
    resting: (when: string | null) => (when ? `嘘——${when} 见` : '嘘——'),
    tomorrow: (time: string) => `明天 ${time}`,
  },
  // 旁白里出现日期时用到的字。
  dateUnits: '年月日',
  waiting: '等米洛来读',
  composerAside: '随便聊～',
  cheer: (length: number) =>
    length === 0
      ? ''
      : length < 40
        ? '嗯嗯，接着说'
        : length < 300
          ? '写得真认真'
          : '好长一页，米洛会慢慢读的',
  emptyThread: '本子还是空的，写第一页吧',
  pinned: '钉住了',
  sceneCaption: '窗边留个位置',
} as const
