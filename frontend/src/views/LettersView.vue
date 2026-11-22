<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import LetterStack from '../components/LetterStack.vue'
import MailboxPlate from '../components/MailboxPlate.vue'
import NewLetterForm from '../components/NewLetterForm.vue'
import PrivateGate from '../components/PrivateGate.vue'
import RunStatus from '../components/RunStatus.vue'
import ScheduleNote from '../components/ScheduleNote.vue'
import { usePrivateSession } from '../composables/usePrivateSession'
import { mailboxPendingLine, sortLetters } from '../utils/letters'
import '../styles/letters.css'
import '../styles/letter-shelf.css'

const router = useRouter()
const {
  user,
  checked,
  conversations,
  runs,
  runsKnown,
  runsLoading,
  runsProblem,
  reloadRuns,
  listed,
  loading,
  problem,
} = usePrivateSession()
const letters = computed(() => sortLetters(conversations.value))

const pendingLine = computed(() => {
  if (loading.value && !letters.value.length) return '正在找回你的信…'
  if (problem.value) return '待回信情况暂时无法确认。'
  return mailboxPendingLine(letters.value)
})

function open(id: string) {
  void router.push(`/letters/${id}`)
}
</script>

<template>
  <!-- 第一次来、还没登录的人看完整介绍；常来的人直接看自己的信箱门牌。 -->
  <section
    v-if="checked && !user"
    class="letters-heading"
    aria-labelledby="letters-title"
  >
    <h1
      id="letters-title"
      class="overprint"
    >
      留封信给米洛
    </h1>
    <p>想说的话放这里。信只有你和米洛看得到，下次来也找得到。</p>
    <ScheduleNote compact />
  </section>

  <div class="letters-workspace">
    <PrivateGate below>
      <MailboxPlate
        v-if="user"
        :name="user.display_name"
        :line="pendingLine"
      >
        <NewLetterForm
          v-if="listed"
          @created="open"
        />
      </MailboxPlate>
      <section
        class="letter-shelf"
        aria-labelledby="shelf-title"
      >
        <h2
          id="shelf-title"
          class="visually-hidden"
        >
          你的信
        </h2>
        <LetterStack
          v-if="listed && letters.length"
          :letters="letters"
        />
        <p
          v-else-if="listed && !problem"
          class="empty-history"
        >
          信箱里现在还没有信，点上面的「开一封新信」就可以写第一封。
        </p>
        <p
          v-else-if="!loading"
          class="empty-history"
        >
          这会儿没读出你的信，读取失败不会弄丢它们。
        </p>
        <p
          v-else
          class="empty-history"
          role="status"
        >
          正在读取信箱目录…
        </p>
      </section>
      <!-- 读信记录不对应某一封信，放在信封下面当脚注。 -->
      <RunStatus
        v-if="listed"
        class="is-footnote"
        :runs="runs"
        :known="runsKnown"
        :loading="runsLoading"
        :problem="runsProblem"
        @retry="reloadRuns"
      />
    </PrivateGate>
  </div>
</template>
