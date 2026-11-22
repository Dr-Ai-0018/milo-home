import type {
  WorkAttachmentList,
  WorkInput,
  WorkReceipt,
  WorkResult,
  WorkTask,
} from '../api/client'
import type { StampedEvent } from '../composables/useWorkTask'

export type WorkDetailPreviewScenario = 'completed-long' | 'running-typing' | 'attachments-failed'

export const WORK_PREVIEW_SCENARIOS: readonly {
  id: WorkDetailPreviewScenario
  label: string
  note: string
}[] = [
  {
    id: 'completed-long',
    label: '已结束 · 长回执与多附件',
    note: '检验长文字回执、多件交付文件、以及原要求/历史就地收折后的阅读主次。',
  },
  {
    id: 'running-typing',
    label: '进行中 · 打字中途完成',
    note: '检验正在输入补话草稿时交办异步结束，输入内容与焦点无损保住。',
  },
  {
    id: 'attachments-failed',
    label: '局部失败 · 附件读取出错',
    note: '检验附件区报错不遮挡状态与整单回执，原地 :aria-disabled 重试可恢复。',
  },
]

export const WORK_REUSE_RULES: readonly { title: string; rule: string }[] = [
  {
    title: '无硬框纸面层级',
    rule: '整张交办单改用 PaperSheet 柔和纸影与虚线分隔，撤掉外层粗边，减少套娃框线。',
  },
  {
    title: '字重与长文行宽',
    rule: '标题字重收敛至 700，长回执与原文限宽 42em、行高 1.75，避免宽屏扫读过长。',
  },
  {
    title: '结果优先、历史就地折展',
    rule: '已结束交办把文字回执与交付附件摆在最前；原交办要求与逐轮记录收进可展开详情，不淹没第一眼。',
  },
  {
    title: '异步结束保草稿与焦点',
    rule: '进行中任务在输入中途转为已完成时，不卸载正在编辑的输入框，保留草稿并用轻量提示引导去向。',
  },
  {
    title: '局部容错与原地重试',
    rule: '附件失败仅提示局部重试且等待时保持 :aria-disabled，键盘焦点不丢失，上面回执照常可读。',
  },
]

const NOW = 1794640800 // 2026-11-14 15:20 CST

const BASE_INITIAL_INPUT: WorkInput = {
  id: '11111111-1111-4111-8111-111111111101',
  task_id: '22222222-2222-4222-8222-222222222242',
  sequence: 1,
  kind: 'initial',
  body: '帮我整理一下近两周小家交办列表与详情页的所有交互修改记录，拆成「已上线变更」「待跟进核对」和「打包带走的原文件」三部分。如果有较长的说明文字，希望先看到结论再看细节过程，不要一打开就被大段记录堆满屏幕。',
  delivery: 'acknowledged',
  created_at: NOW - 5400,
  active_turn: null,
}

const SUPPLEMENT_INPUT: WorkInput = {
  id: '11111111-1111-4111-8111-111111111102',
  task_id: '22222222-2222-4222-8222-222222222242',
  sequence: 2,
  kind: 'supplement',
  body: '另外顺便把移动端 390px 屏宽下按钮会不会换行的问题也列进建议里，方便下轮统一对照检查。',
  delivery: 'acknowledged',
  created_at: NOW - 3900,
  active_turn: null,
}

export const PREVIEW_TASK_COMPLETED: WorkTask = {
  id: '22222222-2222-4222-8222-222222222242',
  session_id: '33333333-3333-4333-8333-333333333301',
  sequence: 42,
  status: 'completed',
  created_at: NOW - 5400,
  updated_at: NOW - 360,
  stop_requested_at: null,
  initial_input: BASE_INITIAL_INPUT,
  deliverable: null,
  active_turn: null,
  execution_available: false,
}

export const PREVIEW_TASK_RUNNING: WorkTask = {
  ...PREVIEW_TASK_COMPLETED,
  sequence: 43,
  status: 'running',
  updated_at: NOW - 45,
}

export const PREVIEW_RECEIPT_LONG: WorkReceipt = {
  state: 'available',
  text: [
    '已经按你的要求整理完毕，三部分关键结论如下：',
    '',
    '1. 列表筛选与定位：交办列表支持按状态筛选（「全部 / 未结束 / 待核对 / 已结束」），从详情返回列表时保留滚动与焦点位置，撤权后自动清空内存缓存。',
    '2. 移动端可读性与控件：在 390px 窄屏下，顶部动作条与附件勾选下载条保持至少 44px 点击高度，长文件名自动折行不撑破版心；补话框在任务异步结束时建议保留草稿与当前焦点。',
    '3. 待跟进核对：下一次真实附件交付产生时，随正常使用顺手复验自动领取、整单回执刷新与 ZIP 打包字节一致即可，不需额外停工。',
    '',
    '下面三个文件已按交付版本单独封存，按需挑单个下载或一次全部打包都可以。',
  ].join('\n'),
}

export const PREVIEW_ATTACHMENTS_FULL: WorkAttachmentList = {
  state: 'available',
  attachments: [
    {
      id: 'att-1',
      filename: '小家交办交互梳理报告-2026-11-14.pdf',
      size_bytes: 428_512,
      sha256: 'a94d8c1f2b3e4567890123456789abcdef1234567890abcdef1234567890abcd',
    },
    {
      id: 'att-2',
      filename: 'mobile-390px-checklist-notes.md',
      size_bytes: 14_920,
      sha256: 'c71e2d3f4a5b6c7d8e9f00112233445566778899aabbccddeeff001122334455',
    },
    {
      id: 'att-3',
      filename: 'work-detail-visual-evidence-bundle.zip',
      size_bytes: 1_892_410,
      sha256: '9f0e1d2c3b4a59687766554433221100fedcba9876543210abcdef0123456789',
    },
  ],
}

export const PREVIEW_INPUTS: readonly WorkInput[] = [BASE_INITIAL_INPUT, SUPPLEMENT_INPUT]

export const PREVIEW_EVENTS_COMPLETED: readonly StampedEvent[] = [
  {
    task_id: PREVIEW_TASK_COMPLETED.id,
    sequence: 1,
    recorded_at: NOW - 5400,
    kind: 'task_status',
    state: 'queued',
    input_id: null,
    result_id: null,
    fresh: false,
  },
  {
    task_id: PREVIEW_TASK_COMPLETED.id,
    sequence: 2,
    recorded_at: NOW - 5320,
    kind: 'task_status',
    state: 'running',
    input_id: null,
    result_id: null,
    fresh: false,
  },
  {
    task_id: PREVIEW_TASK_COMPLETED.id,
    sequence: 3,
    recorded_at: NOW - 3880,
    kind: 'input_delivery',
    state: 'acknowledged',
    input_id: SUPPLEMENT_INPUT.id,
    result_id: null,
    fresh: false,
  },
  {
    task_id: PREVIEW_TASK_COMPLETED.id,
    sequence: 4,
    recorded_at: NOW - 360,
    kind: 'task_status',
    state: 'completed',
    input_id: null,
    result_id: null,
    fresh: false,
  },
]

export const PREVIEW_EVENTS_RUNNING: readonly StampedEvent[] = PREVIEW_EVENTS_COMPLETED.slice(0, 3)

export const PREVIEW_RESULTS: readonly WorkResult[] = [
  {
    id: 'res-1',
    task_id: PREVIEW_TASK_COMPLETED.id,
    input_id: BASE_INITIAL_INPUT.id,
    outcome: 'completed',
    observed_at: NOW - 4100,
    output: {
      answer: {
        state: 'available',
        body: '先整理完了「已上线变更」和目录结构草稿，正在继续补充窄屏布局校对建议。',
        reason: null,
      },
      actions: null,
    },
  },
  {
    id: 'res-2',
    task_id: PREVIEW_TASK_COMPLETED.id,
    input_id: SUPPLEMENT_INPUT.id,
    outcome: 'completed',
    observed_at: NOW - 380,
    output: {
      answer: {
        state: 'available',
        body: '已把 390px 窄屏按钮与长文件名折行条目并入总报告，三个文件已封存完毕。',
        reason: null,
      },
      actions: null,
    },
  },
]
